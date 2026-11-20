"""Cookie vault: atomic writes, one key, locking, host parsing, dot-boundary domain match."""

import multiprocessing
import os
import stat
import sys

import pytest

from agent_wire.auth import cookie_vault
from agent_wire.auth.cookie_vault import CookieVault, _domain_match
from agent_wire.engine.stealth_browser import _domain_match as browser_match


@pytest.mark.parametrize(
    "site", ["x.com:8443", "x.com/path", "https://www.x.com:8443/a?b", "X.COM"]
)
def test_hosts_store_under_registrable_domain(tmp_path, site):
    v = CookieVault(tmp_path)
    v.put(site, {"a": "1"})
    assert v.domains() == ["x.com"] and v.get("x.com") == {"a": "1"}
    assert v.remove(site) and v.domains() == []


@pytest.mark.parametrize("match", [_domain_match, browser_match])
def test_domain_match_needs_a_dot_boundary(match):
    assert match(".example.com", "example.com") and match("www.example.com", "example.com")
    assert match("example.com", "example.com")
    assert not match("evilexample.com", "example.com")
    assert not match("notexample.com", "example.com")


def test_browser_cookie_match_direction():
    assert browser_match("www.example.com", ".example.com")
    assert not browser_match("evilexample.com", ".example.com")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_files_are_private_from_creation(tmp_path):
    v = CookieVault(tmp_path)
    v.put("x.com", {"a": "1"})
    for f in (v.path, v.key_path):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp")) and not (tmp_path / "vault.lock").exists()


def test_save_uses_temp_then_replace(tmp_path, monkeypatch):
    v = CookieVault(tmp_path)
    v.put("x.com", {"a": "1"})
    seen = []
    real = os.replace
    monkeypatch.setattr(
        cookie_vault.os, "replace", lambda a, b: (seen.append((str(a), str(b))), real(a, b))[1]
    )
    v.put("y.com", {"b": "2"})
    assert seen and seen[0][0].endswith(".tmp") and seen[0][1] == str(v.path)
    assert v.domains() == ["x.com", "y.com"]


def test_stale_lock_is_broken_and_live_lock_times_out(tmp_path, monkeypatch):
    v = CookieVault(tmp_path)
    lock = tmp_path / "vault.lock"
    lock.write_text("")
    os.utime(lock, (1, 1))  # a crashed holder
    v.put("x.com", {"a": "1"})
    lock.write_text("")
    monkeypatch.setattr(cookie_vault, "LOCK_WAIT_S", 0.1)
    with pytest.raises(TimeoutError):
        v.put("y.com", {"b": "2"})


def _writer(home, n):
    v = CookieVault(__import__("pathlib").Path(home))
    for i in range(10):
        v.put(f"site{n}x{i}.com", {"k": str(i)})


def test_concurrent_processes_share_one_key_and_lose_no_writes(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(tmp_path), n)) for n in range(3)]
    [p.start() for p in procs]
    [p.join(60) for p in procs]
    assert all(p.exitcode == 0 for p in procs)
    assert len(CookieVault(tmp_path).domains()) == 30
