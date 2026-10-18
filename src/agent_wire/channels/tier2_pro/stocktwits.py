"""StockTwits symbol streams (public API, no key): retail chatter and bull/bear sentiment."""

import re
from collections import Counter

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.models import WireSearchHit

API = "https://api.stocktwits.com/api/2"
_SYMBOL = re.compile(r"^\$?([A-Za-z][A-Za-z0-9.\-]{0,11})$")


def symbol_of(text: str) -> str:
    m = _SYMBOL.match(text.strip().removeprefix("https://stocktwits.com/symbol/").strip("/"))
    if not m:
        raise WireUnsupported("stocktwits: give a ticker like $NVDA or BTC.X")
    return m[1].upper()


def _mood(msg: dict) -> str | None:
    return ((msg.get("entities") or {}).get("sentiment") or {}).get("basic")


def message_hits(data: dict) -> list[WireSearchHit]:
    hits = []
    for msg in data.get("messages") or []:
        user = (msg.get("user") or {}).get("username")
        mood, likes = _mood(msg), (msg.get("likes") or {}).get("total") or 0
        hits.append(WireSearchHit(
            url=f"https://stocktwits.com/{user}/message/{msg.get('id')}",
            title=f"@{user}" + (f" · {mood}" if mood else ""), snippet=msg.get("body") or "",
            source="stocktwits", rank_score=float(likes), timestamp=ts(msg.get("created_at")),
            author=user, engagement={"likes": likes},
        ))  # fmt: skip
    return hits


def sentiment(data: dict) -> str:
    c = Counter(_mood(m) for m in data.get("messages") or [])
    bull, bear = c.get("Bullish", 0), c.get("Bearish", 0)
    return f"{bull} bullish / {bear} bearish" if bull + bear else "no tagged sentiment"


class StockTwitsChannel(Channel):
    name = "stocktwits"

    async def _stream(self, symbol: str):
        return await self.fetch_json(f"{API}/streams/symbol/{symbol}.json")

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        data, _ = await self._stream(symbol_of(query))
        hits = message_hits(data)
        if since:
            hits = [h for h in hits if h.timestamp is None or h.timestamp >= since]
        return hits[:limit]

    async def feed(self, target, *, sort="hot", limit=10):
        if target.strip().lower() in ("trending", "hot"):
            data, f = await self.fetch_json(f"{API}/trending/symbols.json")
            items = [{"title": s.get("symbol"), "url": f"https://stocktwits.com/symbol/{s.get('symbol')}",
                      "meta": [s.get("title"), f"{s.get('watchlist_count', 0):,} watchers"]}
                     for s in (data.get("symbols") or [])[:limit]]  # fmt: skip
            return self.doc(f, "https://stocktwits.com/sentiment", title="StockTwits · trending",
                            body=listing(items))  # fmt: skip
        sym = symbol_of(target)
        data, f = await self._stream(sym)
        info = data.get("symbol") or {}
        items = [{"title": h.title, "url": h.url, "snippet": h.snippet,
                  "meta": [h.timestamp.isoformat(timespec="minutes") if h.timestamp else None]}
                 for h in message_hits(data)[:limit]]  # fmt: skip
        head = f"{info.get('title') or sym} · {info.get('watchlist_count', 0):,} watchers · {sentiment(data)}"
        return self.doc(f, f"https://stocktwits.com/symbol/{sym}", title=f"StockTwits · ${sym}",
                        body=f"{head}\n\n{listing(items)}", metadata={"sentiment": sentiment(data)})  # fmt: skip

    async def read(self, url, *, query=None, section=None, include_comments=True):
        return await self.feed(url, limit=20)


CHANNEL = StockTwitsChannel
