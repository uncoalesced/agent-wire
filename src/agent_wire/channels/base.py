"""Channel interface, shared context and helpers used by every platform module."""

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from selectolax.lexbor import LexborHTMLParser

from agent_wire.engine.http_pool import Fetched, HttpPool
from agent_wire.engine.waterfall import Waterfall
from agent_wire.firewall.inbound_shield import clean_html
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireDocument, WireSearchHit


class WireUnsupported(Exception):
    """The channel can't do this (yet, or without a login); the message says what to do."""


@dataclass
class Context:
    pool: HttpPool
    waterfall: Waterfall
    vault: object | None = None
    ledger: object | None = None
    _channels: dict = field(default_factory=dict)

    def channel(self, name: str) -> "Channel":
        if name not in self._channels:
            from agent_wire.channels import channel_class

            self._channels[name] = channel_class(name)(self)
        return self._channels[name]


class Channel:
    name: ClassVar[str] = ""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def read(
        self, url: str, *, query: str | None = None, section: str | None = None,
        include_comments: bool = True,
    ) -> WireDocument:  # fmt: skip
        raise WireUnsupported(f"{self.name} can't read URLs")

    async def search(
        self, query: str, *, limit: int = 5, sort: str = "relevance"
    ) -> list[WireSearchHit]:
        raise WireUnsupported(f"{self.name} has no search; try platforms=['web']")

    async def feed(self, target: str, *, sort: str = "hot", limit: int = 10) -> WireDocument:
        raise WireUnsupported(f"{self.name} has no feeds")

    async def me(self, target: str = "latest_posts", *, limit: int = 5) -> WireDocument:
        raise WireUnsupported(f"{self.name} has no account inspection")

    # ---- helpers -----------------------------------------------------------------------------
    async def fetch(self, url: str, **kw) -> Fetched:
        kw.setdefault("channel", self.name)
        return await self.ctx.waterfall.fetch(url, **kw)

    async def fetch_json(self, url: str, **kw):
        """API call: no archive/browser stages; identity keys stripped (outbound layer 3)."""
        kw.setdefault("archive", False)
        kw.setdefault("browser", False)
        f = await self.fetch(url, **kw)
        return parse_json(f.content), f

    def doc(self, f: Fetched | None, url: str, **fields) -> WireDocument:
        if f is not None:
            fields.setdefault("stage", f.stage)
            fields.setdefault("auth", f.auth)
            fields.setdefault("raw_bytes", len(f.content))
        return WireDocument(url=url, channel=self.name, **fields)


def ts(value) -> datetime | None:
    """Epoch seconds/ms or ISO-8601 -> aware datetime."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float) or str(value).isdigit():
            v = float(value)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
        return datetime.fromisoformat(str(value))
    except (ValueError, OSError, OverflowError):
        return None


_REL = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
_REL_S = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
          "month": 30 * 86400, "year": 365 * 86400}  # fmt: skip


def relative_ts(text: str | None, now: datetime | None = None) -> datetime | None:
    """'3 weeks ago' / 'Streamed 2 days ago' -> approximate aware datetime."""
    m = _REL.search(text or "")
    if not m:
        return None
    return (now or datetime.now(UTC)) - timedelta(seconds=int(m[1]) * _REL_S[m[2].lower()])


def listing(items: list[dict]) -> str:
    """Compact DAM list for feeds/search: one line per item plus an optional indented snippet."""
    lines = []
    for i, it in enumerate(items, 1):
        bits = [b for b in (it.get("meta") or ()) if b not in (None, "")]
        head = f"{i}. [{it.get('title') or it.get('url')}]({it.get('url')})"
        lines.append(head + (f" · {' · '.join(map(str, bits))}" if bits else ""))
        if snippet := (it.get("snippet") or "").strip():
            lines.append("   " + " ".join(snippet.split())[:280])
    return "\n".join(lines)


def compact_count(text: str | int | None) -> int:
    """'1.2K' / '3,400' / '2M likes' -> int."""
    if text is None:
        return 0
    if isinstance(text, int):
        return text
    m = re.search(r"([\d.,]+)\s*([KMB万亿])?", str(text), re.IGNORECASE)
    if not m:
        return 0
    num = float(m[1].replace(",", "")) if m[1].replace(",", "").replace(".", "").isdigit() else 0
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "万": 1e4, "亿": 1e8}.get((m[2] or "").upper(), 1)
    return int(num * mult)


# ---- HTML -> compact Markdown -------------------------------------------------------------------
_BLOCK = frozenset(
    {"p", "div", "section", "article", "main", "header", "footer", "figure", "details", "summary",
     "dl", "dd", "dt", "tr", "table", "tbody", "thead"}
)  # fmt: skip
_SKIP = frozenset({"img", "svg", "picture", "video", "audio", "iframe", "button", "form", "nav"})
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def html_to_markdown(html: str, *, links: bool = True) -> str:
    """Untrusted HTML fragment -> Markdown, after the inbound shield removed hidden nodes."""
    if not html:
        return ""
    tree = LexborHTMLParser(clean_html(html))
    root = tree.body or tree.root
    out = _md(root, links) if root else ""
    return _BLANKS.sub("\n\n", "\n".join(ln.rstrip() for ln in out.splitlines())).strip()


def _md(node, links: bool) -> str:
    parts = []
    for child in node.iter(include_text=True):
        tag = child.tag
        if tag == "-text":
            parts.append(
                _WS.sub(" ", htmllib.unescape(child.text_content or "")).replace("\n", " ")
            )
        elif tag in _SKIP or tag.startswith("-"):
            continue
        elif tag == "pre":
            parts.append(f"\n\n```\n{child.text(deep=True).strip(chr(10))}\n```\n\n")
        elif tag == "code":
            parts.append(f"`{child.text(deep=True)}`")
        elif tag == "br":
            parts.append("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            parts.append(f"\n\n{'#' * int(tag[1])} {_md(child, links).strip()}\n\n")
        elif tag == "li":
            parts.append(f"\n- {_md(child, links).strip()}")
        elif tag in ("ul", "ol"):
            parts.append(f"\n{_md(child, links)}\n")
        elif tag == "blockquote":
            inner = _md(child, links).strip()
            parts.append("\n\n" + "\n".join(f"> {ln}" for ln in inner.splitlines()) + "\n\n")
        elif tag == "a":
            text = _md(child, links).strip()
            href = child.attributes.get("href") or ""
            use = links and href.startswith("http") and text and text != href
            parts.append(f"[{text}]({href})" if use else text or href)
        elif tag in ("td", "th"):
            parts.append(f" {_md(child, links).strip()} |")
        elif tag == "tr":
            parts.append(f"\n|{_md(child, links)}")
        elif tag in _BLOCK:
            parts.append(f"\n\n{_md(child, links).strip()}\n\n")
        else:
            parts.append(_md(child, links))
    return "".join(parts)


def html_text(html: str) -> str:
    return " ".join(LexborHTMLParser(clean_html(html)).text(separator=" ").split()) if html else ""


# ---- structured data embedded in pages ----------------------------------------------------------
_JSON = json.JSONDecoder()


def meta_tags(html: str) -> dict[str, str]:
    """og:/twitter:/name= meta tags, lower-cased keys."""
    out = {}
    for m in LexborHTMLParser(html).css("meta[property], meta[name]"):
        key = (m.attributes.get("property") or m.attributes.get("name") or "").lower()
        if key and (content := m.attributes.get("content")):
            out.setdefault(key, htmllib.unescape(content))
    return out


def json_ld(html: str) -> list[dict]:
    items = []
    for node in LexborHTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if isinstance(d, dict):
                items += d["@graph"] if isinstance(d.get("@graph"), list) else [d]
    return items


def script_json(html: str, selector: str):
    """JSON inside a <script> matched by CSS selector (e.g. script#__NEXT_DATA__)."""
    node = LexborHTMLParser(html).css_first(selector)
    if not node:
        return None
    try:
        return json.loads(node.text())
    except ValueError:
        return None


def assigned_json(html: str, var: str, *, js_undefined: bool = False):
    """The object literal assigned to `var` in inline JS (`window.__INITIAL_STATE__ = {...}`)."""
    m = re.search(rf"{re.escape(var)}\s*=\s*", html)
    if not m:
        return None
    text = html[m.end() :]
    if js_undefined:  # JS-only literal some sites leave in their state dumps
        text = re.sub(r"([:\[,]\s*)undefined(?=\s*[,}\]])", r"\1null", text)
    try:
        return _JSON.raw_decode(text)[0]
    except ValueError:
        return None
