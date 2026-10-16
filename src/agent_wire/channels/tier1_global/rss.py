"""RSS 2.0 / Atom / JSON Feed reader (stdlib expat: no external entities, billion-laughs guarded)."""

import xml.etree.ElementTree as ET

import orjson

from agent_wire.channels.base import Channel, html_text, listing, ts
from agent_wire.engine.http_pool import Fetched
from agent_wire.models import WireDocument

MAX_ITEMS = 50


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el, *names) -> str:
    c = _child(el, *names)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_feed(content: bytes) -> tuple[str, list[dict]]:
    if content.lstrip().startswith(b"{"):
        data = orjson.loads(content)
        items = [
            {
                "title": it.get("title") or (it.get("content_text") or "")[:80],
                "url": it.get("url") or it.get("external_url") or it.get("id"),
                "date": ts(it.get("date_published")),
                "summary": it.get("summary") or it.get("content_text") or html_text(it.get("content_html", "")),
            }
            for it in data.get("items", [])
        ]  # fmt: skip
        return data.get("title", ""), items

    root = ET.fromstring(content)
    channel = _child(root, "channel")
    if channel is not None:  # RSS 2.0 / 0.9x
        entries, title = [e for e in channel if _local(e.tag) == "item"], _text(channel, "title")
        if not entries:  # RSS 1.0 puts items beside the channel
            entries = [e for e in root if _local(e.tag) == "item"]
    else:  # Atom
        entries, title = [e for e in root if _local(e.tag) == "entry"], _text(root, "title")

    items = []
    for e in entries[:MAX_ITEMS]:
        link = _text(e, "link")
        if not link:
            for c in e:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
        summary = _text(e, "description", "summary", "encoded", "content")
        items.append(
            {
                "title": _text(e, "title"),
                "url": link or _text(e, "guid", "id"),
                "date": ts(_rfc822(_text(e, "pubDate", "published", "updated", "date"))),
                "summary": html_text(summary) if "<" in summary else summary,
            }
        )
    return title, items


def _rfc822(value: str):
    from email.utils import parsedate_to_datetime

    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value  # already ISO-8601 (Atom)


def render_items(items: list[dict]) -> str:
    return listing(
        [
            {
                "title": it["title"],
                "url": it["url"],
                "meta": [it["date"].date().isoformat() if it["date"] else None],
                "snippet": it["summary"],
            }
            for it in items
        ]
    )


class RssChannel(Channel):
    name = "rss"

    def document(self, f: Fetched, url: str, limit: int = MAX_ITEMS) -> WireDocument:
        title, items = parse_feed(f.content)
        return self.doc(f, url, title=title or url, body=render_items(items[:limit]),
                        metadata={"items": len(items)})  # fmt: skip

    async def read(self, url, *, query=None, section=None, include_comments=True):
        return self.document(await self.fetch(url, browser=False), url)

    async def feed(self, target, *, sort="hot", limit=10):
        return self.document(await self.fetch(target, browser=False), target, limit)


CHANNEL = RssChannel
