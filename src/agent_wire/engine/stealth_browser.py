"""Optional stage 4: render in a local headless browser and harvest cf_clearance.

Needs the `stealth` extra (camoufox or playwright). The profile and the browser's own temp
files live in WIRE_HOME, not the OS temp dir.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
from urllib.parse import urlsplit

from agent_wire.engine.challenge import _HARD
from agent_wire.engine.http_pool import Fetched

NAV_TIMEOUT_MS = 30_000
SETTLE_MS = 6_000  # extra wait for XHR-rendered pages; never blocks on long-polling sites
CHALLENGE_WAIT_S = 20  # how long a JS challenge may take to clear itself


def browser_env(home: Path) -> dict[str, str]:
    """Point the browser process's temp files at the tmp folder inside WIRE_HOME."""
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TMP": str(tmp), "TEMP": str(tmp), "TMPDIR": str(tmp)}


class StealthBrowser:
    def __init__(self, home: Path):
        self.home = home
        self.profile_dir = home / "browser-profile"

    @staticmethod
    def available() -> bool:
        return bool(importlib.util.find_spec("camoufox") or importlib.util.find_spec("playwright"))

    async def render(self, url: str) -> tuple[Fetched | None, str | None, str | None]:
        if importlib.util.find_spec("camoufox"):
            from camoufox.async_api import AsyncCamoufox

            async with AsyncCamoufox(
                headless=True, persistent_context=True, user_data_dir=str(self.profile_dir)
            ) as context:
                return await _capture(context, url)

        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(self.profile_dir), headless=True, env=browser_env(self.home),
                downloads_path=str(self.home / "tmp"),
            )  # fmt: skip
            try:
                return await _capture(context, url)
            finally:
                await context.close()


async def _still_challenged(page) -> bool:
    html = (await page.content())[:65_536].lower().encode()
    return any(s in html for s in _HARD)


async def _capture(context, url: str):
    page = await context.new_page()
    response = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
    except Exception:  # noqa: BLE001,S110 - playwright TimeoutError; long-polling never goes idle
        pass
    for _ in range(CHALLENGE_WAIT_S):
        if not await _still_challenged(page):
            break
        await asyncio.sleep(1)  # Cloudflare's JS challenge redirects to the page when solved
    html = await page.content()
    ua = await page.evaluate("navigator.userAgent")
    host = urlsplit(url).hostname or ""
    cf = next(
        (
            c["value"]
            for c in await context.cookies()
            if c["name"] == "cf_clearance" and host.endswith(c["domain"].lstrip("."))
        ),
        None,
    )
    status = response.status if response else 200
    if status in (403, 503) and not await _still_challenged(page):
        status = 200  # the challenge response itself was 403/503; the page we hold now is real
    headers = {"content-type": "text/html; charset=utf-8"}
    return Fetched(page.url, status, headers, html.encode(), requested=url), cf, ua
