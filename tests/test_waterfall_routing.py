import asyncio

import orjson
import pytest

from agent_wire.cache.sqlite_ledger import Ledger
from agent_wire.engine.challenge import is_auth_wall, is_blocked
from agent_wire.engine.http_pool import Fetched, HttpPool, registrable_domain
from agent_wire.engine.waterfall import Waterfall, WireFetchError
from agent_wire.firewall.outbound_privacy import WirePrivacyBlockError

URL = "https://news.example.com/story"
REAL = b"<html><body><article>" + b"Real article content. " * 20 + b"</article></body></html>"
CF = b"<html><head><title>Just a moment...</title></head><body>checking</body></html>"
DATADOME_TAG_PAGE = (
    b"<html><head><script src='https://js.datadome.co/tags.js'></script></head>"
    b"<body>" + b"Normal page text. " * 20 + b"</body></html>"
)


def page(status=200, body=REAL, ctype="text/html; charset=utf-8", url=URL):
    return Fetched(url, status, {"content-type": ctype}, body, requested=url)


class FakePool:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def get(self, url, *, headers=None, cookies=None, impersonate="chrome", params=None,
                  timeout=None, method="GET", json_body=None):  # fmt: skip
        self.calls.append({"url": url, "cookies": cookies, "imp": impersonate, "headers": headers})
        return self.handler(url, cookies=cookies, impersonate=impersonate, params=params)


class FakeVault:
    def __init__(self, jar):
        self.jar = jar
        self.asked = []

    async def cookies_for(self, domain):
        self.asked.append(domain)
        return dict(self.jar)


class FakeLimiter:
    def __init__(self):
        self.waits = []

    async def wait(self, domain):
        self.waits.append(domain)


class FakeBrowser:
    def __init__(self, result):
        self.result = result
        self.urls = []

    def available(self):
        return True

    async def render(self, url):
        self.urls.append(url)
        return self.result


def run(coro):
    return asyncio.run(coro)


def test_stage1_success_is_remembered(tmp_path):
    ledger = Ledger(tmp_path / "c.db")
    f = run(Waterfall(FakePool(lambda u, **k: page()), ledger).fetch(URL))
    assert f.stage == 1 and f.auth == "none"
    assert ledger.get_winning_stage("example.com") == 1


def test_challenge_escalates_to_stage2_and_next_call_starts_there(tmp_path):
    ledger = Ledger(tmp_path / "c.db")
    pool = FakePool(lambda u, impersonate, **k: page(403, CF) if impersonate is None else page())
    wf = Waterfall(pool, ledger)
    assert run(wf.fetch(URL)).stage == 2
    assert ledger.get_winning_stage("example.com") == 2
    pool.calls.clear()
    run(wf.fetch(URL))
    assert pool.calls[0]["imp"] == "chrome"


def test_fingerprint_rotation():
    pool = FakePool(
        lambda u, impersonate, **k: page() if impersonate == "safari" else page(403, CF)
    )
    f = run(Waterfall(pool).fetch(URL))
    assert f.stage == 2
    assert [c["imp"] for c in pool.calls] == [None, "chrome", "safari"]


def test_cookies_never_sent_to_mirrors_or_archives():
    mirror = "https://mirror.example.org/story"

    def handler(url, cookies, **k):
        return page(url=url) if url == mirror else page(403, CF)

    pool = FakePool(handler)
    f = run(Waterfall(pool).fetch(URL, mirrors=[mirror], cookies={"session": "secret"}))
    assert f.stage == 3 and f.auth == "none"
    assert all(c["cookies"] is None for c in pool.calls if c["url"] == mirror)
    assert any(c["cookies"] for c in pool.calls if c["url"] == URL)


def test_cookie_stage_never_reaches_browser():
    browser = FakeBrowser((page(), None, None))
    pool = FakePool(lambda url, **k: page(403, CF) if "archive.org" not in url else page(
        body=b'{"archived_snapshots": {}}', ctype="application/json", url=url))  # fmt: skip
    f = run(Waterfall(pool, browser=browser).fetch(URL, cookies={"session": "secret"}))
    assert f.stage == 4 and f.auth == "none"
    assert browser.urls == [URL]  # the browser gets the URL only, never the cookie jar


def test_wayback_fallback_on_404():
    snap = {"archived_snapshots": {"closest": {"available": True, "timestamp": "20240101000000"}}}

    def handler(url, **k):
        if "archive.org/wayback/available" in url:
            return page(body=orjson.dumps(snap), ctype="application/json", url=url)
        if url.startswith("https://web.archive.org/web/20240101000000id_/"):
            return page(url=url)
        return page(404, b"not found")

    pool = FakePool(handler)
    f = run(Waterfall(pool).fetch(URL))
    assert f.stage == 3 and f.url.startswith("https://web.archive.org/web/")
    # 404 jumps straight to the archive stage: no fingerprint rotation for a dead page
    assert not any(c["imp"] in ("chrome", "safari") for c in pool.calls if c["url"] == URL)


def test_auth_wall_escalates_with_cookies_and_rate_limit():
    vault, limiter = FakeVault({"sid": "abc"}), FakeLimiter()
    pool = FakePool(lambda u, cookies, **k: page() if cookies else page(401, b"login required"))
    f = run(Waterfall(pool, vault=vault, limiter=limiter).fetch(URL))
    assert f.stage == 1 and f.auth == "cookie"
    assert limiter.waits == ["example.com"]
    assert pool.calls[0]["cookies"] is None and pool.calls[1]["cookies"] == {"sid": "abc"}


def test_public_channel_does_not_attach_cookies_up_front():
    vault = FakeVault({"sid": "abc"})
    pool = FakePool(lambda u, **k: page())
    run(
        Waterfall(pool, vault=vault).fetch(
            "https://www.reddit.com/r/x/comments/1", channel="reddit"
        )
    )
    assert vault.asked == [] and pool.calls[0]["cookies"] is None


def test_login_channel_attaches_cookies_up_front():
    vault = FakeVault({"li_at": "abc"})
    pool = FakePool(lambda u, **k: page())
    f = run(Waterfall(pool, vault=vault).fetch("https://www.linkedin.com/in/x", channel="linkedin"))
    assert pool.calls[0]["cookies"] == {"li_at": "abc"} and f.auth == "cookie"


def test_stale_route_falls_back_to_lower_stages(tmp_path):
    ledger = Ledger(tmp_path / "c.db")
    ledger.set_winning_stage("example.com", 3)

    def handler(url, **k):
        if "archive.org" in url:
            return page(body=b'{"archived_snapshots": {}}', ctype="application/json", url=url)
        return page()

    f = run(Waterfall(FakePool(handler), ledger).fetch(URL))
    assert f.stage == 1
    assert ledger.get_winning_stage("example.com") == 1


def test_all_stages_fail_raises_and_forgets_route(tmp_path):
    ledger = Ledger(tmp_path / "c.db")
    ledger.set_winning_stage("example.com", 2)

    def handler(url, **k):
        if "archive.org" in url:
            return page(body=b'{"archived_snapshots": {}}', ctype="application/json", url=url)
        return page(403, CF)

    with pytest.raises(WireFetchError):
        run(Waterfall(FakePool(handler), ledger).fetch(URL))
    assert ledger.get_route("example.com") is None


def test_browser_clearance_is_reused_at_stage2(tmp_path):
    ledger = Ledger(tmp_path / "c.db")
    browser = FakeBrowser((page(), "cf-token", "Mozilla/5.0 Test"))
    state = {"cleared": False}

    def handler(url, cookies, **k):
        if "archive.org" in url:
            return page(body=b'{"archived_snapshots": {}}', ctype="application/json", url=url)
        if state["cleared"] and cookies and cookies.get("cf_clearance") == "cf-token":
            return page()
        return page(403, CF)

    pool = FakePool(handler)
    wf = Waterfall(pool, ledger, browser=browser)
    assert run(wf.fetch(URL)).stage == 4
    route = ledger.get_route("example.com")
    assert route["stage"] == 2 and route["cf_clearance"] == "cf-token"

    state["cleared"] = True
    pool.calls.clear()
    f = run(wf.fetch(URL))
    assert f.stage == 2 and f.auth == "none"  # clearance cookie is not a user session
    assert pool.calls[0]["headers"]["User-Agent"] == "Mozilla/5.0 Test"


def test_private_paths_raise_before_any_network():
    pool = HttpPool()
    with pytest.raises(WirePrivacyBlockError):
        run(pool.get("https://x.com/messages", cookies={"auth_token": "x"}))
    with pytest.raises(WirePrivacyBlockError):
        run(pool.get("https://x.com/i/api/1.1/statuses/update.json", cookies={"a": "b"},
                     method="POST"))  # fmt: skip


@pytest.mark.parametrize(
    "f, blocked",
    [
        (page(403, CF), True),
        (page(200, CF), True),
        (page(503, b"<html><body>Service busy</body></html>"), True),
        (page(200, DATADOME_TAG_PAGE), False),
        (page(200, b"<html><body><div id='root'></div><script>app()</script></body></html>"), True),
        (page(403, b"<html><body>" + b"x" * 40_000 + b"</body></html>"), False),
        (page(200, b'{"ok": true}', ctype="application/json"), False),
        (page(404, b"not found"), False),
    ],
)
def test_challenge_detection(f, blocked):
    assert is_blocked(f) is blocked


def test_auth_wall_detection():
    assert is_auth_wall(page(401, b""))
    assert is_auth_wall(page(200, REAL, url="https://www.linkedin.com/authwall?trk=x"))
    assert not is_auth_wall(page(200, REAL))


def test_registrable_domain():
    assert registrable_domain("www.reddit.com") == "reddit.com"
    assert registrable_domain("old.reddit.com") == "reddit.com"
    assert registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert registrable_domain("localhost") == "localhost"
