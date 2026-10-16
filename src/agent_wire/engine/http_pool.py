"""Persistent curl_cffi connection pool with browser TLS / HTTP2 fingerprints."""

from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit

import orjson
from curl_cffi.requests import AsyncSession

from agent_wire import __version__
from agent_wire.firewall.outbound_privacy import check_request

DEFAULT_TIMEOUT = 12  # a stalled stage should fail fast and let the waterfall escalate
USER_AGENT = f"agent-wire/{__version__} (+https://github.com/uncoalesced/agent-wire)"
MAX_COOKIE_REDIRECTS = 5
_TWO_LEVEL_TLDS = frozenset(
    {"co.uk", "org.uk", "ac.uk", "com.au", "co.in", "co.jp", "com.cn", "com.br", "co.kr", "com.tw"}
)


def registrable_domain(host: str | None) -> str:
    # shortcut: tiny 2-level TLD list instead of the Public Suffix List; upgrade if mis-grouping shows up.
    labels = (host or "").lower().rstrip(".").split(".")
    n = 3 if ".".join(labels[-2:]) in _TWO_LEVEL_TLDS else 2
    return ".".join(labels[-n:])


@dataclass
class Fetched:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    requested: str = ""
    stage: int = 1
    auth: str = "none"

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        msg = Message()
        msg["content-type"] = self.headers.get("content-type", "")
        charset = msg.get_param("charset") or "utf-8"
        try:
            return self.content.decode(str(charset), errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        return orjson.loads(self.content)


class HttpPool:
    """One warm AsyncSession per fingerprint for anonymous traffic.

    Cookie-bearing requests get a throwaway session so the user's session cookies (and anything
    the site sets in reply) never end up in the shared anonymous cookie jars.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self._sessions: dict[str | None, AsyncSession] = {}

    def _session(self, impersonate: str | None) -> AsyncSession:
        if impersonate not in self._sessions:
            self._sessions[impersonate] = AsyncSession(impersonate=impersonate)
        return self._sessions[impersonate]

    async def close(self) -> None:
        for s in self._sessions.values():
            await s.close()
        self._sessions.clear()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        impersonate: str | None = "chrome",
        params: dict | None = None,
        timeout: float | None = None,
        method: str = "GET",
        json_body: dict | None = None,
        data: dict | None = None,
        multipart=None,
    ) -> Fetched:
        check_request(method, url, authenticated=bool(cookies))
        hdrs = dict(headers or {})
        if impersonate is None:
            hdrs.setdefault("User-Agent", USER_AGENT)
        kwargs = {
            "headers": hdrs,
            "params": params,
            "json": json_body,
            "data": data,
            "multipart": multipart,
            "timeout": timeout or self.timeout,
        }
        if not cookies:
            r = await self._session(impersonate).request(
                method, url, allow_redirects=True, max_redirects=8, **kwargs
            )
            return _wrap(r, url)

        # Follow redirects by hand so every hop is re-checked by the privacy jail and
        # cookies never follow a redirect onto another site.
        async with AsyncSession(impersonate=impersonate) as s:
            current = url
            for _ in range(MAX_COOKIE_REDIRECTS + 1):
                r = await s.request(
                    method, current, cookies=cookies, allow_redirects=False, **kwargs
                )
                location = r.headers.get("location")
                if r.status_code not in (301, 302, 303, 307, 308) or not location:
                    return _wrap(r, url)
                nxt = urljoin(current, location)
                if registrable_domain(urlsplit(nxt).hostname) != registrable_domain(
                    urlsplit(url).hostname
                ):
                    return _wrap(r, url)  # cross-site redirect: stop rather than leak cookies
                check_request(method, nxt, authenticated=True)
                current, kwargs["params"] = nxt, None
            return _wrap(r, url)


def _wrap(r, requested: str) -> Fetched:
    headers = {k.lower(): v for k, v in r.headers.items()}
    return Fetched(str(r.url), r.status_code, headers, r.content, requested=requested)
