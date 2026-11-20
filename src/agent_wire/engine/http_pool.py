"""Persistent curl_cffi connection pool with browser TLS / HTTP2 fingerprints."""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit

import orjson
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

from agent_wire import __version__
from agent_wire.engine.errors import WireFetchError
from agent_wire.firewall.outbound_privacy import check_request
from agent_wire.firewall.ssrf_guard import check_resolved

DEFAULT_TIMEOUT = 12  # a stalled stage should fail fast and let the waterfall escalate
USER_AGENT = f"agent-wire/{__version__} (+https://github.com/uncoalesced/agent-wire)"
MAX_REDIRECTS = 8
MAX_COOKIE_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_BODY_ENV = "WIRE_MAX_BODY_MB"
DEFAULT_MAX_BODY_MB = 20
_AUTH_HEADERS = frozenset({"authorization", "x-api-key"})
_TWO_LEVEL_TLDS = frozenset(
    {"co.uk", "org.uk", "ac.uk", "com.au", "co.in", "co.jp", "com.cn", "com.br", "co.kr", "com.tw"}
)


def registrable_domain(host: str | None) -> str:
    # shortcut: tiny 2-level TLD list instead of the Public Suffix List; upgrade if mis-grouping shows up.
    labels = (host or "").lower().rstrip(".").split(".")
    n = 3 if ".".join(labels[-2:]) in _TWO_LEVEL_TLDS else 2
    return ".".join(labels[-n:])


def max_body_bytes() -> int:
    try:
        mb = float(os.environ.get(MAX_BODY_ENV) or DEFAULT_MAX_BODY_MB)
    except ValueError:
        mb = DEFAULT_MAX_BODY_MB
    return int(max(mb, 0.001) * 1024 * 1024)


def strip_auth(headers: dict[str, str] | None) -> dict[str, str]:
    """Credentials headers never travel to another site, a mirror or an archive."""
    return {k: v for k, v in (headers or {}).items() if k.lower() not in _AUTH_HEADERS}


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
        allow_private: bool = False,
    ) -> Fetched:
        """`allow_private` is only for endpoints the user configured (SearXNG, webhooks)."""
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
            return await _follow(self._session(impersonate), method, url, kwargs, cookies=None,
                                 allow_private=allow_private, impersonate=impersonate)  # fmt: skip
        async with AsyncSession(impersonate=impersonate) as s:
            return await _follow(s, method, url, kwargs, cookies=cookies,
                                 allow_private=allow_private, impersonate=impersonate)  # fmt: skip


@asynccontextmanager
async def _for_hop(session, impersonate, pin):
    """The shared session, or a throwaway one whose connection is pinned to the checked IPs."""
    if not pin:
        yield session
        return
    async with AsyncSession(impersonate=impersonate) as s:
        s.curl_options = {CurlOpt.RESOLVE: [pin]}
        yield s


async def _read(r) -> bytes:
    """The body, stopping (and erroring) once it passes the size cap; decoded bytes are counted."""
    cap = max_body_bytes()
    too_big = WireFetchError(f"{r.url}: response larger than {cap >> 20} MB ({MAX_BODY_ENV})")
    declared = r.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap and not r.headers.get("content-encoding"):
        raise too_big
    if not hasattr(r, "aiter_content"):
        if len(r.content) > cap:
            raise too_big
        return r.content
    chunks, size = [], 0
    try:
        async for chunk in r.aiter_content():
            size += len(chunk)
            if size > cap:
                raise too_big
            chunks.append(chunk)
    finally:
        await r.aclose()
    return b"".join(chunks)


async def _follow(session, method, url, kwargs, *, cookies, allow_private,
                  impersonate=None) -> Fetched:  # fmt: skip
    """Follow redirects by hand so every hop is re-checked by the SSRF guard and, with cookies,
    by the privacy jail; cookies never follow a redirect onto another site or down to http, and
    credentials headers never leave the site they were meant for. Each hop connects to the
    addresses the guard just checked."""
    current, hops = url, MAX_COOKIE_REDIRECTS if cookies else MAX_REDIRECTS
    for hop in range(hops + 1):
        pin = None if allow_private else await check_resolved(current)
        async with _for_hop(session, impersonate, pin) as s:
            r = await s.request(method, current, cookies=cookies, allow_redirects=False,
                                stream=True, **kwargs)  # fmt: skip
            location = r.headers.get("location")
            if r.status_code not in REDIRECTS or not location or hop == hops:
                return _wrap(r, url, await _read(r))
            if hasattr(r, "aclose"):
                await r.aclose()
        nxt = urljoin(current, location)
        here, there = urlsplit(current), urlsplit(nxt)
        if cookies:
            if registrable_domain(there.hostname) != registrable_domain(urlsplit(url).hostname):
                return _wrap(r, url, b"")  # cross-site redirect: stop rather than leak cookies
            if here.scheme == "https" and there.scheme == "http":
                return _wrap(r, url, b"")  # never put session cookies on a plaintext hop
            check_request(method, nxt, authenticated=True)
        if registrable_domain(there.hostname) != registrable_domain(here.hostname):
            kwargs = {**kwargs, "headers": strip_auth(kwargs.get("headers"))}
        if r.status_code == 303 or (r.status_code in (301, 302) and method.upper() == "POST"):
            method = "GET"  # what browsers and curl do: the body is not re-sent
            kwargs = {**kwargs, "json": None, "data": None, "multipart": None}
        current, kwargs["params"] = nxt, None


def _wrap(r, requested: str, body: bytes) -> Fetched:
    headers = {k.lower(): v for k, v in r.headers.items()}
    return Fetched(str(r.url), r.status_code, headers, body, requested=requested)
