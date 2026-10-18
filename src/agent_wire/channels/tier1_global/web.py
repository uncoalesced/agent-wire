"""Generic web reader: waterfall fetch -> inbound shield -> trafilatura Markdown -> section slice."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import trafilatura

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, ts
from agent_wire.compression.anchor_slicer import parse_line_anchor, slice_lines, slice_section
from agent_wire.firewall.inbound_shield import clean_html
from agent_wire.models import WireDocument

_TEXT_TYPES = ("text/plain", "text/markdown", "text/x-", "application/json", "application/x-")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")


def is_feed(content_type: str, content: bytes) -> bool:
    if content_type in _FEED_TYPES:
        return True
    head = content[:600].lstrip().lower()
    return content_type in ("text/xml", "application/xml", "") and (
        b"<rss" in head or b"<feed" in head
    )


_DATE_HINT = re.compile(
    r"article:published_time|article:modified_time|datePublished|dateModified|<time[\s>]"
    r"|name=[\"'](?:date|pubdate|publishdate|dc\.date)",
    re.IGNORECASE,
)


def _real_date(date: str | None, html: str) -> str | None:
    """Drop trafilatura's fallback date: with no date markup it reports the crawl day (today)."""
    if not date:
        return None
    today = datetime.now(UTC).date()
    if date[:10] in {(today + timedelta(days=d)).isoformat() for d in (-1, 0, 1)}:
        return date if _DATE_HINT.search(html) else None
    return date


def extract_article(html: str) -> tuple[str, dict]:
    cleaned = clean_html(html)
    body = trafilatura.extract(
        cleaned,
        output_format="markdown",
        include_tables=True,
        include_formatting=True,
        include_comments=False,
        include_links=False,
        include_images=False,
        favor_recall=True,
    )
    meta = trafilatura.extract_metadata(cleaned)
    info = {
        "title": getattr(meta, "title", None),
        "author": getattr(meta, "author", None),
        "date": _real_date(getattr(meta, "date", None), html),
        "site": getattr(meta, "sitename", None),
    }
    body = body or html_to_markdown(cleaned, links=False)
    return body.replace("¶", ""), info  # drop docs permalink glyphs


class WebChannel(Channel):
    name = "web"

    async def read(self, url, *, query=None, section=None, include_comments=True) -> WireDocument:
        f = await self.fetch(url)
        ctype = f.content_type
        if is_feed(ctype, f.content):
            return self.ctx.channel("rss").document(f, url)
        anchor = section or urlsplit(url).fragment or None
        if ctype.startswith(_TEXT_TYPES) or (not ctype and not f.content.lstrip().startswith(b"<")):
            body = f.text
            if lines := parse_line_anchor(url):
                body = slice_lines(body, *lines)
            elif anchor:
                body = slice_section(body, anchor) or body
            return self.doc(f, url, title=urlsplit(url).path.rsplit("/", 1)[-1] or url, body=body)
        if "html" not in ctype and ctype:
            raise WireUnsupported(f"{url} is {ctype}, not a readable page")

        body, info = extract_article(f.text)
        if anchor:
            body = slice_section(body, anchor) or body
        meta = {"site": info["site"]} if info["site"] else {}
        if f.stage == 3 and "web.archive.org" in f.url:
            meta["archived"] = f.url
        return self.doc(
            f, url, title=info["title"], author_handle=info["author"],
            timestamp=ts(info["date"]), body=body, metadata=meta,
        )  # fmt: skip


CHANNEL = WebChannel
