import asyncio

import pytest
from conftest import FakeWaterfall, fetched

from agent_wire.channels.base import Context
from agent_wire.engine import http_pool
from agent_wire.engine.errors import WireBlockedError, WireFetchError
from agent_wire.engine.http_pool import HttpPool
from agent_wire.engine.waterfall import Waterfall
from agent_wire.firewall import ssrf_guard
from agent_wire.firewall.ssrf_guard import check_resolved, check_url
from agent_wire.server import Wire
from agent_wire.verify import citations
from agent_wire.verify.citations import Checker, Citation

PUBLIC = "93.184.215.14"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Offline DNS: `*.internal.test` and `intranet.*` point inward, the rest at a public address."""
    monkeypatch.delenv(ssrf_guard.ALLOW_ENV, raising=False)
    looked = []

    async def resolve(host, port):
        looked.append(host)
        if host == "down.test":
            raise OSError("no such host")
        inward = host.endswith(".internal.test") or host.startswith("intranet.")
        return ["10.1.2.3"] if inward else [PUBLIC]

    monkeypatch.setattr(ssrf_guard, "_resolve", resolve)
    return looked


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://api.localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://172.16.5.4/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://0.0.0.0/",
        "http://127.0.0.1./",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://[::1]/",
        "http://[::]/",
        "http://[fe80::1%25eth0]/",
        "http://[fd00::1]/",
        "http://[ff02::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:a9fe:a9fe]/",
        "http://[64:ff9b::a9fe:a9fe]/",  # NAT64 of the metadata address
        "http://[2002:7f00:1::]/",  # 6to4 of loopback
        "http://2130706433/",  # decimal
        "http://0x7f000001/",  # hex
        "http://0177.0.0.1/",  # octal
        "http://017700000001/",
        "http://0x7f.1/",
        "http://127.1/",
        "http://user:pw@127.0.0.1/",
        "http://evil.example\\@127.0.0.1/",
    ],
)
def test_private_targets_blocked(url):
    with pytest.raises(WireBlockedError, match="blocked"):
        check_url(url)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/x", "gopher://127.0.0.1:6379/_INFO",
     "javascript:alert(1)", "data:text/html,hi", "//example.com/", "http:///nohost"],
)  # fmt: skip
def test_non_web_schemes_blocked(url):
    with pytest.raises(WireBlockedError):
        check_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://example.com/a", f"http://{PUBLIC}/", "http://1.1.1.1/", "http://[2606:4700::1111]/",
     "https://8.8.8.8:443/", "https://0x.example.com/", "https://192.168.example.com/"],
)  # fmt: skip
def test_public_targets_pass(url):
    check_url(url)
    run(check_resolved(url))


def test_blocked_error_is_a_fetch_error():
    with pytest.raises(WireFetchError, match="private address"):
        check_url("http://169.254.169.254/")


def test_dns_names_pointing_inward_blocked(fake_dns):
    with pytest.raises(WireBlockedError, match=r"private address \(10\.1\.2\.3\)"):
        run(check_resolved("https://db.internal.test/"))
    run(check_resolved("https://down.test/"))  # unresolvable: the request itself will fail
    assert fake_dns == ["db.internal.test", "down.test"]


def test_opt_out_env(monkeypatch):
    monkeypatch.setenv(ssrf_guard.ALLOW_ENV, "1")
    check_url("http://localhost:3000/")
    check_url("http://192.168.1.10/wiki")
    run(check_resolved("https://db.internal.test/"))
    with pytest.raises(WireBlockedError):
        check_url("file:///etc/passwd")  # the opt-out is for networks, not schemes


# ---- HttpPool: every redirect hop is re-checked ---------------------------------------------------


class Resp:
    def __init__(self, url, status=200, location=None):
        self.url, self.status_code, self.content = url, status, b"ok"
        self.headers = {"location": location} if location else {}


class FakeSession:
    def __init__(self, hops: dict):
        self.hops = hops
        self.requests = []

    async def request(self, method, url, **kw):
        self.requests.append((method, url, kw.get("json")))
        status, location = self.hops.get(url, (200, None))
        return Resp(url, status, location)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def pool_with(monkeypatch, hops):
    session = FakeSession(hops)
    pool = HttpPool()
    monkeypatch.setattr(pool, "_session", lambda impersonate: session)
    monkeypatch.setattr(http_pool, "AsyncSession", lambda impersonate=None: session)
    return pool, session


@pytest.mark.parametrize(
    "target",
    ["http://169.254.169.254/latest/meta-data/", "http://db.internal.test/", "file:///etc/passwd"],
)
def test_redirect_into_private_network_blocked(monkeypatch, target):
    pool, session = pool_with(monkeypatch, {"https://example.com/go": (302, target)})
    with pytest.raises(WireBlockedError):
        run(pool.get("https://example.com/go"))
    assert [u for _, u, _ in session.requests] == ["https://example.com/go"]


def test_same_site_redirect_with_cookies_blocked(monkeypatch):
    pool, session = pool_with(
        monkeypatch, {"https://example.com/a": (301, "https://intranet.example.com/")}
    )
    with pytest.raises(WireBlockedError):
        run(pool.get("https://example.com/a", cookies={"sid": "x"}))
    assert len(session.requests) == 1


def test_public_redirects_followed(monkeypatch):
    pool, session = pool_with(
        monkeypatch,
        {
            "https://example.com/a": (301, "/b"),
            "https://example.com/b": (303, "https://other.org/c"),
        },
    )
    f = run(pool.get("https://example.com/a", method="POST", json_body={"k": 1}))
    assert f.url == "https://other.org/c" and f.requested == "https://example.com/a"
    assert session.requests == [("POST", "https://example.com/a", {"k": 1}),
                                ("GET", "https://example.com/b", None),
                                ("GET", "https://other.org/c", None)]  # fmt: skip


def test_direct_private_request_never_sent(monkeypatch):
    pool, session = pool_with(monkeypatch, {})
    with pytest.raises(WireBlockedError):
        run(pool.get("http://[::ffff:10.0.0.1]/"))
    assert session.requests == []


def test_allow_private_for_user_configured_endpoints(monkeypatch):
    pool, session = pool_with(monkeypatch, {})
    f = run(pool.get("http://localhost:8888/search?q=x", allow_private=True))
    assert f.status == 200 and len(session.requests) == 1


# ---- callers -------------------------------------------------------------------------------------


class RecordingPool:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append(url)
        return fetched("<html><body>" + "page text " * 50 + "</body></html>", url=url)


def test_waterfall_blocks_before_any_stage():
    pool = RecordingPool()
    with pytest.raises(WireBlockedError):
        run(Waterfall(pool).fetch("http://127.0.0.1:9000/", mirrors=["https://mirror.example/x"]))
    assert pool.calls == []  # not the URL, a mirror, or the Wayback lookup


def make_wire(tmp_path, routes):
    w = Wire(home=tmp_path)
    w.ctx = Context(pool=None, waterfall=FakeWaterfall(routes), vault=w.vault, ledger=w.ledger)
    return w


def test_wire_read_returns_clean_error(tmp_path):
    w = make_wire(tmp_path, {"": "<html><body>secret</body></html>"})
    out = run(w.read(["http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"]))
    assert out.count("WireBlockedError") == 2 and "secret" not in out
    assert "private address" in out and "only http(s)" in out
    assert w.ctx.waterfall.calls == []
    run(w.close())


def test_code_mode_read_data_blocked(tmp_path):
    w = make_wire(tmp_path, {"": "<html><body>secret</body></html>"})
    data, _ = run(w.read_data(["http://0x7f000001/"]))
    assert "blocked: private address" in str(data) and "secret" not in str(data)
    assert w.ctx.waterfall.calls == []
    run(w.close())


def test_citation_to_private_address_reported_blocked():
    waterfall = FakeWaterfall({"": "<html><body>internal dashboard</body></html>"})
    c = run(Checker(waterfall, None).check(Citation("http://192.168.0.1/status")))
    assert c.verdict == "BLOCKED" and c.note == citations.PRIVATE_NOTE
    assert waterfall.calls == []
    report, _ = citations.render([c], [])
    assert "BLOCKED http://192.168.0.1/status" in report and "private or internal" in report


def test_citation_to_name_resolving_inward_reported_blocked():
    def raise_(url, kw):
        raise WireBlockedError(f"{url}: blocked: private address (10.1.2.3)")

    c = run(
        Checker(FakeWaterfall({"": raise_}), None).check(Citation("https://wiki.internal.test/"))
    )
    assert c.verdict == "BLOCKED" and c.archive is None
