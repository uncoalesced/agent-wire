"""Detect Cloudflare / DataDome / Turnstile challenges, login walls and empty JS shells."""

from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from agent_wire.engine.http_pool import Fetched

BLOCK_STATUSES = frozenset({401, 403, 429, 503})
SMALL_BODY = 35_000
MIN_VISIBLE_CHARS = 80

# Only ever present on a challenge page, so safe to match anywhere near the top.
_HARD = (
    b"<title>just a moment...</title>",
    b"cf-browser-verification",
    b"cf_chl_opt",
    b"captcha-delivery.com",  # DataDome block page (its normal tag script lives on js.datadome.co)
)
# Also appear on ordinary pages (articles about them, login forms), so only on small bodies.
_SOFT = (
    b"challenges.cloudflare.com/turnstile",
    b"<title>access denied</title>",
    b"enable javascript and cookies to continue",
)
_LOGIN_PATHS = ("/login", "/signin", "/sign-in", "/accounts/login", "/i/flow/login", "/authwall")


def visible_text_len(html: bytes) -> int:
    tree = LexborHTMLParser(html)
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    body = tree.body
    return len(body.text(strip=True)) if body else 0


def is_blocked(f: Fetched, *, rendered: bool = False) -> bool:
    """`rendered=True` for stage-4 output: JS already ran, so a short page is just short."""
    small = len(f.content) < SMALL_BODY
    if f.status in BLOCK_STATUSES and small:
        return True
    head = f.content[:65_536].lower()
    if any(s in head for s in _HARD):
        return True
    if small and any(s in head for s in _SOFT):
        return True
    return bool(
        not rendered
        and f.ok
        and f.content
        and "html" in f.content_type
        and visible_text_len(f.content) < MIN_VISIBLE_CHARS
    )


def is_auth_wall(f: Fetched) -> bool:
    path = urlsplit(f.url).path.lower()
    return f.status in (401, 403) or any(path.startswith(p) for p in _LOGIN_PATHS)
