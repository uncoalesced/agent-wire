"""Encrypted session vault (vault.enc). The only source of cookies agent-wire ever sends.

Sessions get in only when the user runs `wire auth login <site>`, either through a real login
window or by explicitly importing that one site from a local browser (`--from-browser`).
Nothing reads browser cookie stores on its own. Cookie values never leave this module except
as request cookies; nothing here logs them.
"""

import asyncio
import ctypes
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import orjson
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_wire.auth.browser_paths import find_cookie_stores
from agent_wire.engine.http_pool import registrable_domain

LOGIN_POLL_S = 1.0  # how often the login window's cookies are snapshotted
LOCK_WAIT_S = 10.0
LOCK_STALE_S = 30.0


def _reg(site: str) -> str:
    """Registrable domain of a bare host, `host:port`, `host/path` or a full URL."""
    host = urlsplit(site if "//" in site else f"//{site}").hostname
    return registrable_domain(host or site)


def _domain_match(cookie_domain: str, reg: str) -> bool:
    d = cookie_domain.lstrip(".").lower()
    return d == reg or d.endswith("." + reg)


try:  # no wheel for every Python yet; Chromium extraction is skipped without it
    import rookiepy
except ImportError:  # pragma: no cover - depends on platform wheels
    rookiepy = None


class CookieVault:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "vault.enc"
        self.key_path = home / "vault.key"

    # ---- encrypted vault -------------------------------------------------------------------
    def _key(self) -> bytes:
        for _ in range(100):
            if self.key_path.exists() and (raw := self.key_path.read_bytes()):
                return _unprotect(raw)
            self.home.mkdir(parents=True, exist_ok=True)
            try:  # O_EXCL: of two processes racing to create the key, exactly one wins
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                time.sleep(0.02)  # the winner is still writing; read its key next round
                continue
            key = AESGCM.generate_key(bit_length=256)
            with os.fdopen(fd, "wb") as f:
                f.write(_protect(key))
            return key
        raise OSError("vault key is unreadable")

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        blob = self.path.read_bytes()
        return orjson.loads(AESGCM(self._key()).decrypt(blob[:12], blob[12:], None))

    def _save(self, data: dict) -> None:
        nonce = os.urandom(12)
        blob = nonce + AESGCM(self._key()).encrypt(nonce, orjson.dumps(data), None)
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from byte one
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # readers see the old vault or the new one, never half
        finally:
            tmp.unlink(missing_ok=True)

    @contextmanager
    def _locked(self):
        """One writer at a time across processes: an atomically created lock file."""
        lock = self.home / "vault.lock"
        self.home.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > LOCK_STALE_S:  # a crashed holder
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError("cookie vault is locked by another process") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def put(self, domain: str, cookies: dict[str, str]) -> None:
        with self._locked():
            data = self._load()
            data[_reg(domain)] = cookies
            self._save(data)

    def get(self, domain: str) -> dict[str, str]:
        return self._load().get(_reg(domain), {})

    def remove(self, domain: str) -> bool:
        with self._locked():
            data = self._load()
            found = data.pop(_reg(domain), None) is not None
            self._save(data)
        return found

    def domains(self) -> list[str]:
        return sorted(self._load())

    # ---- lookup ----------------------------------------------------------------------------
    async def cookies_for(self, domain: str) -> dict[str, str]:
        """Sessions saved with `wire auth login` only; browsers are never read implicitly."""
        return self.get(domain)

    async def import_from_browser(self, site: str) -> int:
        """`wire auth login <site> --from-browser`: copy one site's cookies, on explicit request."""
        reg = _reg(site)
        jar = await asyncio.to_thread(browser_cookies, reg)
        if jar:
            self.put(reg, jar)
        return len(jar)

    async def interactive_login(self, url: str) -> int:
        """Open a real browser window once; save the cookies for the site when it is closed."""
        from playwright.async_api import async_playwright  # needs the `stealth` extra

        profile = self.home / "login-profile"
        reg = _reg(url)
        jar: dict[str, str] = {}
        async with async_playwright() as p:
            from agent_wire.engine.stealth_browser import browser_env

            ctx = await p.chromium.launch_persistent_context(
                str(profile), headless=False, env=browser_env(self.home)
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url if "//" in url else f"https://{url}")
            # Closing the window tears the whole context down, so cookies can't be read after
            # it: keep a fresh snapshot while it's open and save the last one.
            while not page.is_closed():
                try:
                    jar = {c["name"]: c["value"] for c in await ctx.cookies()
                           if _domain_match(c["domain"], reg)}  # fmt: skip
                except Exception:  # noqa: BLE001 - TargetClosedError as the window goes away
                    break
                await asyncio.sleep(LOGIN_POLL_S)
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001,S110 - already closed with the window
                pass
        shutil.rmtree(profile, ignore_errors=True)  # don't leave an unencrypted session on disk
        if jar:
            self.put(reg, jar)
        return len(jar)


def browser_cookies(reg: str) -> dict[str, str]:
    for store in find_cookie_stores():
        if store.kind == "firefox" and (jar := _firefox_cookies(store.path, reg)):
            return jar
    if rookiepy is not None:
        try:
            rows = rookiepy.load(domains=[reg])
        except Exception:  # noqa: BLE001 - v20 app-bound keys, locked keychains: rookiepy raises bare errors
            rows = []
        jar = {r["name"]: r["value"] for r in rows if r.get("value")}
        if jar:
            return jar
    return {}


def _firefox_cookies(db: Path, reg: str) -> dict[str, str]:
    # immutable=1: read while Firefox is running without taking (or waiting on) its lock.
    uri = f"{db.as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute(
                "SELECT name, value FROM moz_cookies WHERE host = ? OR host = ? OR host LIKE ?"
                " ORDER BY lastAccessed",
                (reg, f".{reg}", f"%.{reg}"),
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {name: value for name, value in rows if value}


# ---- OS key protection -------------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _protect(key: bytes) -> bytes:
    # shortcut: macOS/Linux keep the key in a 0600 file; move to Keychain/libsecret if needed.
    return _dpapi(key, True) if sys.platform == "win32" else key


def _unprotect(blob: bytes) -> bytes:
    return _dpapi(blob, False) if sys.platform == "win32" else blob
