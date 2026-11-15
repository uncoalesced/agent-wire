# Engineered by uncoalesced
"""Shield events: what the firewall did to a page or a request, recorded per tool call.

Firewall layers call `record(...)` wherever they strip, escape, refuse or flag something. The
active `Recorder` (one per tool call, held in a ContextVar so asyncio tasks share it) collects
the events; the server turns T1/T2 ones into alerts. With no recorder active, `record` is a
no-op, so library and test use of the firewall costs nothing extra.
"""

import re
import secrets
import time
from collections import Counter
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit, urlunsplit

T0, T1, T2 = 0, 1, 2  # info (counted only), alert, high
TIER_NAMES = {T0: "info", T1: "alert", T2: "high"}

# Honest action vocabulary: never say "blocked" for text that was only marked.
STRIPPED = "stripped"  # removed before the model saw it
ESCAPED = "escaped"  # neutralized in place (e.g. a forged delimiter)
REFUSED = "refused"  # the request was never sent
FLAGGED = "flagged-only"  # visible text: wrapped as untrusted data and flagged, not removable

SESSION_ID = secrets.token_hex(4)
SNIPPET_CHARS = 120

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[\]PX^_].*?(?:\x07|\x1b\\)|\x1b.", re.DOTALL)
# C0/C1 controls, bidi controls and isolates, zero-width and tag characters.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)


def sanitize(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Attacker text made safe to show in a terminal, a toast or a log line."""
    from agent_wire.firewall.outbound_privacy import scrub  # local: firewall imports this module

    text = _UNSAFE.sub(" ", _ANSI.sub("", text or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return scrub(text)


def scrub_url(url: str) -> str:
    """Drop credentials, query values and the fragment: keep where, not what was sent."""
    if not url:
        return ""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
    except ValueError:
        return sanitize(url, 200)
    keys = [kv.split("=", 1)[0] for kv in p.query.split("&") if kv]
    query = "&".join(f"{k}=…" for k in keys)
    return sanitize(urlunsplit((p.scheme, host, p.path, query, "")), 300)


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


@dataclass
class Alert:
    layer: str  # inbound-html | inbound-text | outbound | vault | code-mode
    rule_id: str
    tier: int
    technique: str
    action: str
    url: str = ""
    tool: str = ""
    channel: str = ""
    location: str = ""
    snippet: str = ""
    decoded: str = ""
    bytes_removed: int = 0
    session_id: str = SESSION_ID
    ts: float = field(default_factory=time.time)

    def as_row(self) -> dict:
        return asdict(self)


class Recorder:
    """Collects the events of one tool call."""

    def __init__(self, tool: str, verbose: bool = False):
        self.tool = tool
        self.verbose = verbose
        self.alerts: list[Alert] = []
        self.counts: Counter[str] = Counter()  # T0 rule_id -> how many
        self._seen: set[tuple] = set()

    def add(self, a: Alert) -> None:
        key = (a.rule_id, a.url, a.snippet, a.decoded)
        if key in self._seen:  # the same page is often cleaned more than once
            return
        self._seen.add(key)
        a.tool = a.tool or self.tool
        if a.tier == T0:
            self.counts[a.rule_id] += 1
            if not self.verbose:
                return
        self.alerts.append(a)

    @property
    def raised(self) -> list[Alert]:
        return [a for a in self.alerts if a.tier >= T1]


current: ContextVar[Recorder | None] = ContextVar("wire_shield_recorder", default=None)
current_url: ContextVar[str] = ContextVar("wire_shield_url", default="")


def active() -> bool:
    return current.get() is not None


def record(layer: str, rule_id: str, tier: int, technique: str, action: str, *,
           url: str = "", location: str = "", snippet: str = "", decoded: str = "",
           bytes_removed: int = 0, channel: str = "") -> None:  # fmt: skip
    rec = current.get()
    if rec is None:
        return
    rec.add(Alert(layer=layer, rule_id=rule_id, tier=tier, technique=technique, action=action,
                  url=scrub_url(url or current_url.get()), location=sanitize(location, 160),
                  snippet=sanitize(snippet), decoded=sanitize(decoded),
                  bytes_removed=bytes_removed, channel=channel))  # fmt: skip
