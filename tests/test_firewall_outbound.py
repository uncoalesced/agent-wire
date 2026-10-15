import statistics
import time

import pytest

from agent_wire.firewall.outbound_privacy import (
    WirePrivacyBlockError,
    check_request,
    parse_json,
    scrub,
    should_attach_cookies,
)


def test_public_first_cookie_policy():
    assert not should_attach_cookies("reddit")
    assert not should_attach_cookies("github")
    assert should_attach_cookies("reddit", tool="wire_me")
    assert should_attach_cookies("linkedin")
    assert should_attach_cookies("x_search")
    assert should_attach_cookies("web", auth_wall=True)


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/messages",
        "https://x.com/messages/123-456",
        "https://www.reddit.com/settings/privacy",
        "https://github.com/me/repo/settings",
        "https://www.facebook.com/ads_manager",
    ],
)
def test_authenticated_private_paths_blocked(url):
    with pytest.raises(WirePrivacyBlockError):
        check_request("GET", url, authenticated=True)


def test_authenticated_writes_blocked():
    with pytest.raises(WirePrivacyBlockError):
        check_request("POST", "https://x.com/i/api/graphql/abc/CreateTweet", authenticated=True)


def test_allowed_requests_pass():
    check_request("GET", "https://x.com/someone/status/1", authenticated=True)
    check_request("HEAD", "https://x.com/notifications", authenticated=True)
    check_request("GET", "https://github.com/settings-sync/app", authenticated=True)
    # anonymous requests can't reach private data, so public /security pages stay readable
    check_request("GET", "https://www.python.org/security", authenticated=False)


def test_sensitive_keys_dropped_recursively():
    raw = b'{"data": {"Viewer": {"id": 1}, "post": {"title": "t", "email": "a@b.co"}}, "items": [{"csrf_token": "x", "ok": 1}]}'
    assert parse_json(raw) == {"data": {"post": {"title": "t"}}, "items": [{"ok": 1}]}


def test_scrub_secrets():
    text = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def\n"
        "Cookie: auth_token=abc123; ct0=zzz\n"
        "fetch with li_at=AQEDAR-secret&x=1 and cf_clearance=Xyz.123"
    )
    out = scrub(text)
    for secret in ("eyJhbGci", "abc123", "zzz", "AQEDAR-secret", "Xyz.123"):
        assert secret not in out
    assert "Bearer [REDACTED]" in out
    assert "&x=1" in out


def test_scrub_private_ips_only():
    out = scrub(
        "lan 192.168.1.5, vpn 10.0.0.7, ula fd00::1, dns 8.8.8.8, local 127.0.0.1 [::1] 0.0.0.0"
    )
    assert "192.168.1.5" not in out and "10.0.0.7" not in out and "fd00::1" not in out
    assert "8.8.8.8" in out and "127.0.0.1" in out and "[::1]" in out and "0.0.0.0" in out


def test_scrub_leaves_lookalikes():
    text = "at 12:30:45 use std::vector and a[::2]; version 1.2.3"
    assert scrub(text) == text


def test_emails_redacted_only_when_authenticated():
    assert scrub("contact maintainer@example.org") == "contact maintainer@example.org"
    assert scrub("hi me@example.org", authenticated=True) == "hi [EMAIL]"


def test_known_secrets():
    assert (
        scrub("logged in as joel_handle", known_secrets=("joel_handle",))
        == "logged in as [REDACTED]"
    )


def test_outbound_speed():
    doc = "Some public paragraph with https://example.com/page and numbers 3.14 and 2024. " * 250
    runs = []
    for _ in range(50):
        t = time.perf_counter()
        check_request("GET", "https://www.reddit.com/r/LocalLLaMA/comments/abc", authenticated=True)
        scrub(doc)
        runs.append(time.perf_counter() - t)
    median_ms = statistics.median(runs) * 1000
    print(f"outbound firewall on {len(doc) // 1000} KB: {median_ms:.3f} ms")
    assert median_ms < 5
