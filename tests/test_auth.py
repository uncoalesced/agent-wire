import asyncio
import sqlite3
import sys

import pytest

from agent_wire.auth import cookie_vault
from agent_wire.auth.browser_paths import find_cookie_stores
from agent_wire.auth.cookie_vault import CookieVault, _firefox_cookies
from agent_wire.auth.rate_limiter import RateLimiter


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def kinds(stores):
    return {(s.browser, s.kind) for s in stores}


def test_windows_stores(tmp_path):
    local, roaming = tmp_path / "AppData" / "Local", tmp_path / "AppData" / "Roaming"
    touch(local / "Google/Chrome/User Data/Default/Network/Cookies")
    touch(local / "Google/Chrome/User Data/Profile 2/Network/Cookies")
    touch(local / "Microsoft/Edge/User Data/Default/Cookies")  # legacy location
    touch(roaming / "Opera Software/Opera GX Stable/Network/Cookies")  # profile-less
    touch(roaming / "Mozilla/Firefox/Profiles/abc.default-release/cookies.sqlite")
    touch(roaming / "zen/Profiles/z1.Default/cookies.sqlite")
    touch(
        local
        / "Packages/TheBrowserCompany.Arc_ttt/LocalCache/Local/Arc/User Data/Default/Network/Cookies"
    )
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    stores = find_cookie_stores(tmp_path, "win32", env)
    assert kinds(stores) == {
        ("chrome", "chromium"), ("edge", "chromium"), ("opera_gx", "chromium"),
        ("firefox", "firefox"), ("zen", "firefox"), ("arc", "chromium"),
    }  # fmt: skip
    assert sum(s.browser == "chrome" for s in stores) == 2


def test_linux_flatpak_and_snap(tmp_path):
    touch(
        tmp_path / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cookies"
    )
    touch(tmp_path / "snap/firefox/common/.mozilla/firefox/p1.default/cookies.sqlite")
    touch(tmp_path / ".config/chromium/Default/Cookies")
    stores = find_cookie_stores(tmp_path, "linux", {})
    assert kinds(stores) == {
        ("brave", "chromium"),
        ("firefox", "firefox"),
        ("chromium", "chromium"),
    }


def test_macos_safari_and_chrome(tmp_path):
    touch(tmp_path / "Library/Cookies/Cookies.binarycookies")
    touch(tmp_path / "Library/Application Support/Google/Chrome/Default/Cookies")
    stores = find_cookie_stores(tmp_path, "darwin", {})
    assert kinds(stores) == {("safari", "safari"), ("chrome", "chromium")}


def test_firefox_cookies_read_only(tmp_path):
    db = tmp_path / "cookies.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, lastAccessed INT)"
        )
        conn.executemany(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
            [
                (".reddit.com", "reddit_session", "s1", 1),
                ("www.reddit.com", "token", "t1", 2),
                (".notreddit.com", "evil", "e", 3),
                (".example.com", "other", "o", 4),
            ],
        )
    assert _firefox_cookies(db, "reddit.com") == {"reddit_session": "s1", "token": "t1"}
    assert _firefox_cookies(tmp_path / "missing.sqlite", "reddit.com") == {}


def test_vault_roundtrip_is_encrypted(tmp_path):
    vault = CookieVault(tmp_path)
    vault.put("www.x.com", {"auth_token": "SUPERSECRET", "ct0": "csrf"})
    assert vault.get("x.com") == {"auth_token": "SUPERSECRET", "ct0": "csrf"}
    assert vault.domains() == ["x.com"]
    assert b"SUPERSECRET" not in (tmp_path / "vault.enc").read_bytes()
    if sys.platform == "win32":
        assert len((tmp_path / "vault.key").read_bytes()) > 32  # DPAPI-wrapped, not the raw key
    assert CookieVault(tmp_path).get("x.com")["ct0"] == "csrf"  # new instance, same key
    assert vault.remove("x.com") and vault.domains() == []


def test_vault_wins_over_browsers(tmp_path, monkeypatch):
    vault = CookieVault(tmp_path)
    vault.put("reddit.com", {"reddit_session": "v"})

    def boom(reg):
        raise AssertionError("browsers must not be read when the vault has the site")

    monkeypatch.setattr(cookie_vault, "browser_cookies", boom)
    assert asyncio.run(vault.cookies_for("old.reddit.com")) == {"reddit_session": "v"}


def test_falls_back_to_browsers(tmp_path, monkeypatch):
    monkeypatch.setattr(cookie_vault, "browser_cookies", lambda reg: {"from": reg})
    assert asyncio.run(CookieVault(tmp_path).cookies_for("www.bilibili.com")) == {
        "from": "bilibili.com"
    }


def test_rate_limiter_paces_per_domain():
    now = [100.0]
    slept = []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(clock=lambda: now[0], sleep=sleep)

    async def go():
        await limiter.wait("x.com")
        await limiter.wait("reddit.com")
        await limiter.wait("x.com")

    asyncio.run(go())
    assert len(slept) == 1
    assert 2.15 <= slept[0] <= 2.65


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_roundtrip():
    blob = cookie_vault._protect(b"k" * 32)
    assert blob != b"k" * 32 and cookie_vault._unprotect(blob) == b"k" * 32
