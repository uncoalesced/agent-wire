"""SSRF host decoding, DNS pinning, body cap, deadline and credential-header redirects."""

import asyncio

import pytest
from curl_cffi import CurlOpt

from agent_wire.engine import http_pool, waterfall
from agent_wire.engine.errors import WireBlockedError, WireFetchError
from agent_wire.engine.http_pool import Fetched, HttpPool
from agent_wire.engine.waterfall import Waterfall
from agent_wire.firewall import ssrf_guard
from agent_wire.firewall.ssrf_guard import check_resolved, check_url

PUBLIC = "93.184.215.14"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.delenv(ssrf_guard.ALLOW_ENV, raising=False)
    answers = {"calls": [], "ips": [[PUBLIC]]}

    async def resolve(host, port):
        answers["calls"].append(host)
        seq = answers["ips"]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(ssrf_guard, "_resolve", resolve)
    return answers


@pytest.mark.parametrize(
    "url",
    ["http://%6cocalhost:8080/", "http://%6Cocalhost/", "http://%256cocalhost/",
     "http://api.%6cocalhost/", "http://%6detadata.google.internal/", "http://a%2eb%2ec/",
     "http://%31%32%37.0.0.1/", "http://%2531%2532%2537.0.0.1/", "http://exa%6dple.com/"],
)  # fmt: skip
def test_percent_encoded_hosts_blocked(url):
    with pytest.raises(WireBlockedError):
        check_url(url)
    with pytest.raises(WireBlockedError):
        run(check_resolved(url))


def test_check_resolved_returns_pin(dns):
    assert run(check_resolved("https://example.com/")) == f"example.com:443:{PUBLIC}"
    assert run(check_resolved("http://example.com:8080/")) == f"example.com:8080:{PUBLIC}"
    assert run(check_resolved(f"http://{PUBLIC}/")) is None


# ---- fake session ----------------------------------------------------------------------------


class Resp:
    def __init__(self, url, status=200, location=None, body=b"ok"):
        self.url, self.status_code, self.content = url, status, body
        self.headers = {"location": location} if location else {}


class StreamResp(Resp):
    def __init__(self, url, chunks):
        super().__init__(url)
        self._chunks = chunks

    async def aiter_content(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        pass


class Session:
    def __init__(self, hops=None, make=None):
        self.hops, self.make, self.requests, self.curl_options = hops or {}, make, [], {}

    async def request(self, method, url, **kw):
        self.requests.append((url, kw.get("headers"), kw.get("cookies"), dict(self.curl_options)))
        if self.make:
            return self.make(url)
        status, location = self.hops.get(url, (200, None))
        return Resp(url, status, location)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def pool_with(monkeypatch, session):
    pool = HttpPool()
    monkeypatch.setattr(pool, "_session", lambda impersonate: session)
    monkeypatch.setattr(http_pool, "AsyncSession", lambda impersonate=None: session)
    return pool


def test_connection_pinned_and_rebind_blocked_on_hop(monkeypatch, dns):
    dns["ips"] = [[PUBLIC], ["10.0.0.5"]]  # first answer public, then the name flips inward
    s = Session({"https://a.example.com/x": (302, "https://b.example.com/y")})
    pool = pool_with(monkeypatch, s)
    with pytest.raises(WireBlockedError, match="10.0.0.5"):
        run(pool.get("https://a.example.com/x"))
    ((_u, _h, _c, opts),) = s.requests  # the first hop connected to the checked address only
    assert opts == {CurlOpt.RESOLVE: [f"a.example.com:443:{PUBLIC}"]}


# ---- size cap --------------------------------------------------------------------------------


def test_body_cap_streamed(monkeypatch):
    monkeypatch.setenv(http_pool.MAX_BODY_ENV, "1")
    chunks = [b"x" * 600_000] * 3
    pool = pool_with(monkeypatch, Session(make=lambda u: StreamResp(u, chunks)))
    with pytest.raises(WireFetchError, match="larger than 1 MB"):
        run(pool.get("https://example.com/big"))


def test_body_cap_non_streaming_and_declared(monkeypatch):
    monkeypatch.setenv(http_pool.MAX_BODY_ENV, "1")
    pool = pool_with(monkeypatch, Session(make=lambda u: Resp(u, body=b"x" * 2_000_000)))
    with pytest.raises(WireFetchError):
        run(pool.get("https://example.com/big"))

    def declared(u):
        r = StreamResp(u, [b"x"])
        r.headers["content-length"] = "99999999"
        return r

    pool = pool_with(monkeypatch, Session(make=declared))
    with pytest.raises(WireFetchError):
        run(pool.get("https://example.com/big"))


def test_body_under_cap_read_whole(monkeypatch):
    pool = pool_with(monkeypatch, Session(make=lambda u: StreamResp(u, [b"ab", b"cd"])))
    assert run(pool.get("https://example.com/")).content == b"abcd"


# ---- deadline and browser slots ---------------------------------------------------------------


class SlowPool:
    async def get(self, *a, **kw):
        await asyncio.sleep(5)


def test_waterfall_deadline(monkeypatch):
    monkeypatch.setenv(waterfall.DEADLINE_ENV, "0.1")
    with pytest.raises(WireFetchError, match="gave up after 0.1s"):
        run(Waterfall(SlowPool()).fetch("https://example.com/"))


def test_browser_launches_limited(monkeypatch):
    live = peak = 0

    class Browser:
        def available(self):
            return True

        async def render(self, url):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1
            return Fetched(url, 200, {}, b"<html>" + b"x" * 600 + b"</html>"), None, None

    async def go():
        wf = Waterfall(SlowPool(), browser=Browser())
        await asyncio.gather(
            *(
                wf._run(4, "https://e.com", "e.com", None, None, (), {}, True, True)
                for _ in range(5)
            )
        )

    run(go())
    assert peak == 2


# ---- credential headers on redirects -----------------------------------------------------------


def test_authorization_dropped_cross_site_kept_same_site(monkeypatch):
    s = Session(
        {
            "https://api.example.com/a": (302, "https://cdn.example.com/b"),
            "https://cdn.example.com/b": (302, "https://evil.org/c"),
        }
    )
    pool = pool_with(monkeypatch, s)
    hdrs = {"Authorization": "Bearer t", "X-Api-Key": "k", "Accept": "x"}
    run(pool.get("https://api.example.com/a", headers=hdrs))
    sent = {u: h for u, h, _c, _o in s.requests}
    assert sent["https://cdn.example.com/b"].keys() >= {"Authorization", "X-Api-Key"}
    assert sent["https://evil.org/c"] == {"Accept": "x"}


def test_no_cookies_on_https_to_http_downgrade(monkeypatch):
    s = Session({"https://example.com/a": (302, "http://example.com/b")})
    pool = pool_with(monkeypatch, s)
    run(pool.get("https://example.com/a", cookies={"sid": "x"}))
    assert [u for u, *_ in s.requests] == ["https://example.com/a"]


def test_mirrors_get_no_auth(monkeypatch):
    seen = []

    class Pool:
        async def get(self, url, headers=None, **kw):
            seen.append((url, headers))
            if "mirror" in url:
                return Fetched(url, 200, {"content-type": "text/html"}, b"<p>" + b"hello " * 200)
            return Fetched(url, 403, {}, b"no")

    f = run(
        Waterfall(Pool()).fetch(
            "https://example.com/",
            mirrors=["https://mirror.test/x"],
            headers={"authorization": "Bearer t", "Accept": "x"},
            browser=False,
        )
    )
    assert f.url == "https://mirror.test/x"
    assert seen[-1] == ("https://mirror.test/x", {"Accept": "x"})
