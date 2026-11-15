"""Offline stand-ins for the network layer, shared by `wire selftest` and the test suite."""

import contextlib
import ipaddress
import socket

import orjson

from agent_wire.engine.http_pool import Fetched


def fetched(body, url="https://example.com/", status=200, ctype=None, requested=None) -> Fetched:
    if isinstance(body, dict | list):
        body, ctype = orjson.dumps(body), ctype or "application/json"
    elif isinstance(body, str):
        body = body.encode()
    return Fetched(url, status, {"content-type": ctype or "text/html; charset=utf-8"}, body,
                   requested=requested or url)  # fmt: skip


class FakeWaterfall:
    """Routes by first matching URL substring; records every call with its kwargs."""

    limiter = None

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def fetch(self, url, **kw):
        self.calls.append((url, kw))
        for needle, resp in self.routes.items():
            if needle in url:
                resp = resp(url, kw) if callable(resp) else resp
                if not isinstance(resp, Fetched):
                    resp = fetched(resp, url=url)
                return resp
        from agent_wire.engine.waterfall import WireFetchError

        raise WireFetchError(f"no fake route for {url}")


class FakePool:
    """Stands in for HttpPool where channels need one shared cookie session. Unknown URLs: 404."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.calls: list[str] = []

    async def get(self, url, **kw):
        self.calls.append(url)
        for needle, resp in self.routes.items():
            if needle in url:
                return resp if hasattr(resp, "status") else fetched(resp, url=url)
        return fetched(b"not found", url=url, status=404)

    async def close(self):
        return None


class FakeVault:
    def __init__(self, jar=None):
        self.jar = jar or {}

    def get(self, domain):
        return self.jar.get(domain, {})

    async def cookies_for(self, domain):
        return self.jar.get(domain, {})

    def domains(self):
        return sorted(self.jar)


class NetworkUsed(AssertionError):
    """Raised when an offline check tries to reach the network."""


def _loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@contextlib.contextmanager
def no_network(attempts: list[str]):
    """Block every way agent-wire reaches the network; append what was tried to `attempts`.

    curl_cffi does its I/O inside libcurl, below Python's socket module, so the HTTP pool's session
    class is replaced directly. Python-level sockets are also blocked, except loopback: asyncio's
    self-pipe on Windows is a loopback socket pair.
    """
    from agent_wire.engine import http_pool

    def refuse(what: str):
        attempts.append(what)
        raise NetworkUsed(f"offline selftest tried the network: {what}")

    class NoSession:
        def __init__(self, *a, **kw):
            refuse(f"curl_cffi session {kw.get('impersonate')}")

    real_session, real_get = http_pool.AsyncSession, http_pool.HttpPool.get
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    async def pool_get(self, url, **kw):
        refuse(f"HttpPool.get {url}")

    def connect(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", None) or _loopback(address):
            return real_connect(sock, address)
        refuse(f"socket connect {address}")

    def getaddrinfo(host, *a, **kw):
        if _loopback(host if isinstance(host, str) else ""):
            return real_getaddrinfo(host, *a, **kw)
        refuse(f"DNS lookup {host}")

    http_pool.AsyncSession, http_pool.HttpPool.get = NoSession, pool_get
    socket.socket.connect, socket.getaddrinfo = connect, getaddrinfo
    try:
        yield attempts
    finally:
        http_pool.AsyncSession, http_pool.HttpPool.get = real_session, real_get
        socket.socket.connect, socket.getaddrinfo = real_connect, real_getaddrinfo
