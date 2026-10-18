"""News: Google News RSS search (date-windowed) and the Techmeme river, no keys."""

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from agent_wire.channels.base import Channel, WireUnsupported, html_text, listing
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.models import WireSearchHit

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
TECHMEME = "https://www.techmeme.com/feed.xml"
_HREF = re.compile(r'href="(https?://[^"]+)"')
_WORD = re.compile(r"[a-z0-9+#]+")
_OUTLET = re.compile(r"\s*\((?:[A-Z][\w.&' -]{1,40})\)\s*$")  # "... (Bloomberg)"


def _date(text: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(text) if text else None
    except (TypeError, ValueError):
        return None


def parse_rss_items(content: bytes) -> list[dict]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise WireUnsupported(f"news feed unreadable ({e})") from e
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        out.append({"title": (it.findtext("title") or "").strip(), "url": it.findtext("link") or "",
                    "date": _date(it.findtext("pubDate")), "desc": it.findtext("description") or "",
                    "source": src.text if src is not None else None})  # fmt: skip
    return out


def gnews_hits(items: list[dict]) -> list[WireSearchHit]:
    hits = []
    for it in items:
        title, outlet = it["title"], it["source"]
        if outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3]
        hits.append(WireSearchHit(url=it["url"], title=title, snippet=outlet or "", source="news",
                                  timestamp=it["date"], author=outlet))  # fmt: skip
    return hits


def techmeme_hits(items: list[dict]) -> list[WireSearchHit]:
    hits = []
    for it in items:
        links = [u for u in _HREF.findall(it["desc"]) if "techmeme.com" not in u]
        hits.append(WireSearchHit(url=it["url"], title=_OUTLET.sub("", it["title"]), snippet=html_text(it["desc"])[:300],
                                  source="techmeme", timestamp=it["date"],
                                  link=links[0] if links else None))  # fmt: skip
    return hits


def matches(hit: WireSearchHit, query: str) -> bool:
    """Every content word of the query appears in the title or snippet."""
    words = [w for w in _WORD.findall(query.lower()) if len(w) > 1]
    text = f"{hit.title} {hit.snippet}".lower()
    return bool(words) and all(w in text for w in words)


class NewsChannel(Channel):
    name = "news"

    async def _items(self, url: str) -> list[dict]:
        f = await self.fetch(url, archive=False, browser=False)
        return parse_rss_items(f.content)

    async def techmeme(self, query: str | None = None, since=None) -> list[WireSearchHit]:
        hits = techmeme_hits(await self._items(TECHMEME))
        if since:
            hits = [h for h in hits if h.timestamp is None or h.timestamp >= since]
        return [h for h in hits if matches(h, query)] if query else hits

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        q = query
        if since:
            q = f"{query} when:{max(1, (datetime.now(UTC) - since).days)}d"
        hits = gnews_hits(await self._items(GNEWS.format(q=quote(q))))
        if sort in ("new", "date", "recent"):
            hits.sort(key=lambda h: h.timestamp or datetime.min.replace(tzinfo=UTC), reverse=True)
        try:
            tm = await self.techmeme(query, since)
        except (WireFetchError, WireUnsupported):  # Techmeme is a bonus; Google News answered
            tm = []
        return (tm + hits)[:limit]

    async def feed(self, target="techmeme", *, sort="hot", limit=10):
        key = target.strip().lower()
        if key in ("techmeme", "tech", ""):
            hits = (await self.techmeme())[:limit]
            title, url = "Techmeme", "https://www.techmeme.com/"
        else:
            hits = await self.search(
                target, limit=limit, since=datetime.now(UTC) - timedelta(days=7)
            )
            title, url = f"News · {target}", GNEWS.format(q=quote(target))
        items = [{"title": h.title, "url": h.link or h.url, "snippet": h.snippet,
                  "meta": [h.timestamp.date().isoformat() if h.timestamp else None]} for h in hits]  # fmt: skip
        return self.doc(None, url, title=title, body=listing(items))


CHANNEL = NewsChannel
