"""Dense Agent Markdown: compact document + comment-tree rendering and the [WIRE | ...] envelope."""

import re
from datetime import UTC, datetime

from agent_wire.compression.url_compactor import compact
from agent_wire.firewall.inbound_shield import spotlight
from agent_wire.models import WireComment, WireDocument, WireTelemetry

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLANKS = re.compile(r"\n{3,}")
_AGE_UNITS = (("y", 31_536_000), ("mo", 2_592_000), ("d", 86_400), ("h", 3_600), ("m", 60))


def telemetry_header(t: WireTelemetry) -> str:
    return (
        f"[WIRE | {t.channel} | {t.tokens} tokens (saved {t.saved_pct}%) | auth:{t.auth}"
        f" | stage:{t.stage} | {t.elapsed_ms}ms]"
    )


def envelope(t: WireTelemetry, content: str) -> str:
    return spotlight(telemetry_header(t), content)


def relative_age(ts: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = max(0, int((now - ts).total_seconds()))
    for unit, size in _AGE_UNITS:
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def format_comments(
    comments: list[WireComment], now: datetime | None = None, depth: int = 0
) -> str:
    lines = []
    prefix = "  " * depth + ("> " if depth else "")
    for c in comments:
        tags = (
            [f"{c.score} replies" if c.score_kind == "replies" else f"{c.score:+d}"]
            + ["OP"] * c.is_op
            + ["ACCEPTED"] * c.is_accepted
        )
        who = f"@{c.author_handle}" if c.author_handle else "@[deleted]"
        age = f" ({relative_age(c.timestamp, now)})" if c.timestamp else ""
        body = [ln for ln in c.body.strip().splitlines() if ln.strip()] or [""]
        lines.append(f"{prefix}[{' | '.join(tags)}] {who}{age}: {body[0]}")
        lines.extend(" " * len(prefix) + ln for ln in body[1:])
        if c.replies:
            lines.append(format_comments(c.replies, now, depth + 1))
    return "\n".join(lines)


def render_document(doc: WireDocument, now: datetime | None = None) -> str:
    parts = [f"# {doc.title}"] if doc.title else []
    meta = []
    if doc.author_handle:
        meta.append(f"@{doc.author_handle}")
    if doc.timestamp:
        meta.append(doc.timestamp.date().isoformat())
    if doc.score is not None:
        meta.append(f"{doc.score:+d}")
    meta += [f"{k}: {v}" for k, v in doc.metadata.items() if v is not None]
    if meta:
        parts.append(" · ".join(meta))
    if body := _IMAGE.sub("", doc.body).strip():
        parts.append(body)
    if doc.comments:
        parts.append("## Comments\n" + format_comments(doc.comments, now))
    return compact(_BLANKS.sub("\n\n", "\n\n".join(parts)))
