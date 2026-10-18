"""4-stage anti-bot waterfall with per-domain memory of the stage that worked."""

from collections.abc import Sequence
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException

from agent_wire.engine.challenge import is_auth_wall, is_blocked
from agent_wire.engine.http_pool import Fetched, HttpPool, registrable_domain
from agent_wire.firewall.outbound_privacy import should_attach_cookies

IMPERSONATE_ROTATION = ("chrome", "safari", "chrome_android")
STAGES = (1, 2, 3, 4)


class WireFetchError(Exception):
    pass


class Waterfall:
    def __init__(self, pool: HttpPool, ledger=None, vault=None, limiter=None, browser=None):
        self.pool = pool
        self.ledger = ledger
        self.vault = vault
        self.limiter = limiter
        self.browser = browser

    async def fetch(
        self,
        url: str,
        *,
        channel: str = "web",
        mirrors: Sequence[str] = (),
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        tool: str = "wire_read",
        archive: bool = True,
        browser: bool = True,
        method: str = "GET",
        json_body: dict | None = None,
    ) -> Fetched:
        domain = registrable_domain(urlsplit(url).hostname)
        route = self.ledger.get_route(domain) if self.ledger else None
        start = route["stage"] if route else 1
        if cookies is None and self.vault and should_attach_cookies(channel, tool=tool):
            cookies = await self.vault.cookies_for(domain) or None
        req = {"headers": headers, "method": method, "json_body": json_body}

        # Remembered stage first; if it has gone stale, fall back through the rest in order.
        order = [s for s in STAGES if s >= start] + [s for s in STAGES if s < start]
        last: Fetched | None = None
        errors: list[str] = []
        archive_only = False
        for stage in order:
            if archive_only and stage != 3:
                continue
            try:
                f, cf, ua = await self._run(stage, url, domain, route, cookies, mirrors, req,
                                            archive, browser)  # fmt: skip
                if (
                    f is not None
                    and stage <= 2
                    and is_auth_wall(f)
                    and not cookies
                    and self.vault
                    and (cookies := await self.vault.cookies_for(domain) or None)
                ):
                    f, cf, ua = await self._run(stage, url, domain, route, cookies, mirrors,
                                                req, archive, browser)  # fmt: skip
            except RequestException as e:
                errors.append(f"stage {stage}: {type(e).__name__}")
                continue
            except Exception as e:
                if stage != 4:
                    raise
                errors.append(f"stage 4: {type(e).__name__}")
                continue
            if f is None:
                continue
            if f.ok and not is_blocked(f, rendered=stage == 4):
                f.stage = stage
                f.auth = "cookie" if cookies and stage <= 2 else "none"
                self._remember(domain, stage, cf, ua)
                return f
            last = f
            if f.status in (404, 410):
                archive_only = True  # really gone: only an archive can help

        if self.ledger and route:
            self.ledger.clear_route(domain)
        detail = f"HTTP {last.status}" if last else "; ".join(errors) or "no stage succeeded"
        raise WireFetchError(f"{url}: all waterfall stages failed ({detail})")

    async def _run(self, stage, url, domain, route, cookies, mirrors, req, archive, browser):
        """Return (response, cf_clearance, user_agent) for one stage, or (None, ...) if skipped."""
        if stage <= 2 and cookies and self.limiter:
            await self.limiter.wait(domain)  # pace only the user's own sessions
        if stage == 1:
            return await self._get(url, cookies, None, req), None, None
        if stage == 2:
            hdrs = dict(req["headers"] or {})
            jar = dict(cookies or {})
            if route and route.get("cf_clearance"):
                jar["cf_clearance"] = route["cf_clearance"]
                hdrs["User-Agent"] = route["user_agent"] or hdrs.get("User-Agent", "")
            f = None
            for profile in IMPERSONATE_ROTATION:
                f = await self._get(url, jar or None, profile, {**req, "headers": hdrs})
                if f.ok and not is_blocked(f):
                    break
            return f, None, None
        if stage == 3:  # never send the user's cookies to mirrors or archives
            for mirror in mirrors:
                try:
                    f = await self.pool.get(mirror, headers=req["headers"], impersonate="chrome")
                except RequestException:
                    continue
                if f.ok and not is_blocked(f):
                    return f, None, None
            return (await self._wayback(url) if archive else None), None, None
        if stage == 4 and browser and self.browser and self.browser.available():
            return await self.browser.render(url)
        return None, None, None

    async def _get(self, url, cookies, impersonate, req) -> Fetched:
        return await self.pool.get(
            url,
            headers=req["headers"],
            cookies=cookies,
            impersonate=impersonate,
            method=req["method"],
            json_body=req["json_body"],
        )

    async def _wayback(self, url: str) -> Fetched | None:
        snapshot = await wayback_snapshot(self.pool, url)
        return await self.pool.get(snapshot, impersonate=None) if snapshot else None

    def _remember(self, domain: str, stage: int, cf: str | None, ua: str | None) -> None:
        if not self.ledger:
            return
        # A browser that harvested cf_clearance lets later requests run at stage-2 speed.
        self.ledger.set_winning_stage(domain, 2 if cf else stage, cf_clearance=cf, user_agent=ua)


async def wayback_snapshot(pool: HttpPool, url: str) -> str | None:
    """Raw (`id_`) URL of the closest Wayback Machine snapshot, if any."""
    try:
        f = await pool.get(
            "https://archive.org/wayback/available", params={"url": url}, impersonate=None
        )
        closest = f.json().get("archived_snapshots", {}).get("closest") if f.ok else None
    except (RequestException, ValueError):
        return None
    if not closest or not closest.get("available"):
        return None
    return f"https://web.archive.org/web/{closest['timestamp']}id_/{url}"
