"""4-layer outbound privacy firewall: keeps the user's cookies, identity and PII away from the LLM."""

import ipaddress
import re
from urllib.parse import urlsplit

import orjson


class WirePrivacyBlockError(Exception):
    pass


# Layer 1: public-first. Cookies only for wire_me, login-only endpoints, or after an auth wall.
LOGIN_REQUIRED = frozenset({"x_search", "linkedin", "xiaohongshu", "instagram"})


def should_attach_cookies(
    channel: str, *, tool: str = "wire_read", auth_wall: bool = False
) -> bool:
    return tool == "wire_me" or channel in LOGIN_REQUIRED or auth_wall


# Layer 2: method + private-path jail for cookie-bearing requests.
BLOCKED_PRIVATE_PATHS = (
    "/messages", "/dms", "/inbox", "/chat", "/settings",
    "/account", "/billing", "/payments", "/security",
    "/password", "/sessions", "/ads_manager",
)  # fmt: skip
_BLOCKED_SEGMENTS = frozenset(p.lstrip("/") for p in BLOCKED_PRIVATE_PATHS)


def check_request(method: str, url: str, *, authenticated: bool) -> None:
    """Raise before any packet leaves if a cookie-bearing request could touch private data.

    Anonymous requests can't reach private account data, so public pages like /security
    stay readable; the jail applies whenever cookies are attached (incl. auth-wall escalation).
    """
    if not authenticated:
        return
    if method.upper() not in ("GET", "HEAD"):
        raise WirePrivacyBlockError(f"authenticated {method} blocked: read-only only")
    segments = urlsplit(url).path.lower().split("/")
    if blocked := _BLOCKED_SEGMENTS.intersection(segments):
        raise WirePrivacyBlockError(f"private account path blocked: /{min(blocked)}")


# Layer 3: drop identity keys from parsed JSON before mapping onto models.
SENSITIVE_KEYS = frozenset(
    {
        "viewer", "current_user", "self_profile", "logged_in_user",
        "email", "phone", "csrf_token", "auth_token", "session_id",
    }
)  # fmt: skip


def strip_sensitive_keys(obj):
    if isinstance(obj, dict):
        return {
            k: strip_sensitive_keys(v) for k, v in obj.items() if k.lower() not in SENSITIVE_KEYS
        }
    if isinstance(obj, list):
        return [strip_sensitive_keys(v) for v in obj]
    return obj


def parse_json(raw: bytes | str):
    return strip_sensitive_keys(orjson.loads(raw))


# Layer 4: scrubber for reflected secrets, private IPs and (when authenticated) emails.
# Separate guarded passes: one big alternation makes `re` try every branch at every
# character, ~4x slower on a 20 KB page.
_SECRET_NAMES = (
    "auth_token", "ct0", "li_at", "jsessionid", "sessionid", "session_id", "reddit_session",
    "cf_clearance", "web_session", "sessdata", "__secure-", "__host-",
)  # fmt: skip
_SECRET = re.compile(
    r"(\bBearer\s+)[A-Za-z0-9\-._~+/]{8,}=*"
    r"|(\b(?:Set-)?Cookie:[ \t]*)[^\r\n]+"
    r"|(\b(?:auth_token|ct0|li_at|JSESSIONID|sessionid|session_id|reddit_session"
    r"|cf_clearance|web_session|SESSDATA|__Secure-[\w-]+|__Host-[\w-]+)=)[^;\s\"'&]+",
    re.IGNORECASE,
)
# Well-known API-key formats a page might quote (leaked keys in issues, pastes, docs).
# Prefixed formats only, so ordinary words and hashes stay readable.
_KEY_HINTS = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "sk-", "_live_", "AKIA", "ASIA",
    "xox", "AIza", "glpat-", "hf_", "npm_",
)  # fmt: skip
_API_KEY = re.compile(
    r"(?<![\w-])(?:"
    r"gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|sk-(?:ant-|proj-)?(?=[\w-]*\d)[A-Za-z0-9_-]{20,}"  # needs a digit: skip prose slugs
    r"|[sr]k_live_[A-Za-z0-9]{20,}"
    r"|A(?:KIA|SIA)[A-Z0-9]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{35}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|npm_[A-Za-z0-9]{36}"
    r")(?![\w-])"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
# Charset-first patterns let `re` skip ahead fast; boundaries are checked in _ip_repl instead.
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_IPV6 = re.compile(r"[0-9A-Fa-f:]*:[0-9A-Fa-f]*:[0-9A-Fa-f:]*")


def _is_private_ip(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False  # times like 12:30:45, version strings, etc.
    # Loopback / 0.0.0.0 appear in every tutorial and identify nobody.
    return ip.is_private and not (ip.is_loopback or ip.is_unspecified)


def _ip_repl(m: re.Match) -> str:
    s, text = m[0], m.string
    before = text[m.start() - 1] if m.start() else " "
    after = text[m.end()] if m.end() < len(text) else " "
    if before.isalnum() or before in "._" or after.isalnum() or after in "._":
        return s  # part of a longer token: std::vector, 1.2.3.4.5
    return "[PRIVATE_IP]" if _is_private_ip(s) else s


def scrub(text: str, *, authenticated: bool = False, known_secrets: tuple[str, ...] = ()) -> str:
    lower = text.lower()
    if "bearer" in lower or "cookie" in lower or any(n in lower for n in _SECRET_NAMES):
        text = _SECRET.sub(lambda m: next(g for g in m.groups() if g) + "[REDACTED]", text)
    if any(h in text for h in _KEY_HINTS):
        text = _API_KEY.sub("[REDACTED]", text)
    if authenticated and "@" in text:
        text = _EMAIL.sub("[EMAIL]", text)
    text = _IPV4.sub(_ip_repl, text)
    text = _IPV6.sub(_ip_repl, text)
    for secret in known_secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text
