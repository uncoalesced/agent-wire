"""News: Google News RSS search (date-windowed) and the Techmeme river, no keys.

Google News items link to news.google.com/rss/articles/<id>, which only redirects with JavaScript.
decode() resolves the publisher URL the way that page does: read its signature + timestamp, then
ask the batchexecute endpoint for the target.
"""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from curl_cffi.requests.exceptions import RequestException

from agent_wire.channels.base import Channel, WireUnsupported, html_text, listing
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.models import WireSearchHit

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
GN_ARTICLE = re.compile(r"^https://news\.google\.com/(?:rss/)?(?:articles|read)/([\w-]+)")
BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_SG = re.compile(rb'data-n-a-sg="([^"]+)"')
_TS = re.compile(rb'data-n-a-ts="(\d+)"')
DECODE_TIMEOUT_S = 10
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


def batch_form(gid: str, stamp: str, sig: str) -> dict:
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
                             None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
             gid, int(stamp), sig]  # fmt: skip
    return {"f.req": json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])}


def parse_batch(text: str) -> str | None:
    try:
        rows = json.loads(text.split("\n\n", 1)[1])
        url = json.loads(rows[0][2])[1]
    except (IndexError, ValueError, TypeError):
        return None
    return url if isinstance(url, str) and url.startswith("http") else None


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
        out = (tm + hits)[:limit]
        real = await asyncio.gather(*(self.decode(h.url) for h in out))
        return [h.model_copy(update={"url": u}) for h, u in zip(out, real, strict=True)]

    async def decode(self, url: str) -> str:
        """Publisher URL behind a Google News link; the link itself when it can't be resolved."""
        m = GN_ARTICLE.match(url)
        if not m or self.ctx.pool is None:
            return url
        try:
            return await asyncio.wait_for(self._decode(m[1]), DECODE_TIMEOUT_S) or url
        except (RequestException, TimeoutError, ValueError):
            return url

    async def _decode(self, gid: str) -> str | None:
        page = await self.ctx.pool.get(f"https://news.google.com/rss/articles/{gid}")
        sig, stamp = _SG.search(page.content), _TS.search(page.content)
        if not (sig and stamp):
            return None
        r = await self.ctx.pool.get(
            BATCH, method="POST", data=batch_form(gid, stamp[1].decode(), sig[1].decode()),
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )  # fmt: skip
        return parse_batch(r.content.decode("utf-8", "replace")) if r.ok else None

    async def read(self, url, *, query=None, section=None, include_comments=True):
        from agent_wire.router import route_url  # router imports the channel registry

        target = await self.decode(url)
        if target == url:
            raise WireUnsupported(f"news: could not resolve the Google News link {url}")
        return await self.ctx.channel(route_url(target)).read(
            target, query=query, section=section, include_comments=include_comments
        )

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
