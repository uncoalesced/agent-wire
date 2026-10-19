"""Polymarket prediction markets (public Gamma API, no key): what money says about a topic."""

import json
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.models import WireSearchHit

GAMMA = "https://gamma-api.polymarket.com"
EVENT = "https://polymarket.com/event/"


def _odds(market: dict) -> str:
    """'Yes 62% · No 38%' from the JSON-in-a-string outcome fields."""
    try:
        names = json.loads(market.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return ""
    label = market.get("groupItemTitle")
    odds = " · ".join(f"{n} {round(p * 100)}%" for n, p in zip(names, prices, strict=False))
    return f"{label}: {odds}" if label else odds


def _movement(market: dict) -> str:
    d = market.get("oneWeekPriceChange") or market.get("oneDayPriceChange")
    if not isinstance(d, int | float) or not d:
        return ""
    return f"({'+' if d > 0 else ''}{round(d * 100)} pts)"


def event_summary(event: dict, max_markets: int = 4) -> list[str]:
    markets = sorted(event.get("markets") or [], key=lambda m: -float(m.get("volume") or 0))
    lines = (" ".join(filter(None, (_odds(m), _movement(m)))) for m in markets[:max_markets])
    return [line for line in lines if line]


def event_hit(event: dict) -> WireSearchHit:
    vol24 = int(float(event.get("volume24hr") or 0))
    return WireSearchHit(
        url=EVENT + event.get("slug", ""), title=event.get("title") or "",
        snippet=" | ".join(event_summary(event, 3)), source="polymarket",
        rank_score=float(vol24),  # undated: a market is live until it resolves, so "freshness" is moot
        engagement={"volume": int(float(event.get("volume") or 0)), "volume24h": vol24,
                    "liquidity": int(float(event.get("liquidity") or 0)),
                    "comments": event.get("commentCount") or 0},
    )  # fmt: skip


class PolymarketChannel(Channel):
    name = "polymarket"

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        data, _ = await self.fetch_json(
            f"{GAMMA}/public-search?q={quote(query)}&limit_per_type={max(limit, 5)}&events_status=active"
        )
        events = [e for e in data.get("events") or [] if not e.get("closed")]
        if since:  # markets that resolved before the window opened are history, not news
            events = [e for e in events if (t := ts(e.get("endDate"))) is None or t >= since]
        return [event_hit(e) for e in events[:limit]]

    async def feed(self, target="trending", *, sort="hot", limit=10):
        if target.strip().lower() not in ("trending", "hot", "top", "movers", ""):
            hits = await self.search(target, limit=limit)
            body = listing([{"title": h.title, "url": h.url, "snippet": h.snippet,
                             "meta": [f"${h.engagement['volume24h']:,} 24h"]} for h in hits])  # fmt: skip
            return self.doc(None, f"https://polymarket.com/search?q={quote(target)}",
                            title=f"Polymarket · {target}", body=body)  # fmt: skip
        data, f = await self.fetch_json(
            f"{GAMMA}/events?limit={limit}&active=true&closed=false&order=volume24hr&ascending=false"
        )
        items = [{"title": e.get("title"), "url": EVENT + e.get("slug", ""),
                  "meta": [f"${int(float(e.get('volume24hr') or 0)):,} 24h"],
                  "snippet": " | ".join(event_summary(e, 2))} for e in data]  # fmt: skip
        return self.doc(
            f, "https://polymarket.com/", title="Polymarket · trending", body=listing(items)
        )

    async def trending_hits(self, limit=10) -> list[WireSearchHit]:
        data, _ = await self.fetch_json(
            f"{GAMMA}/events?limit={limit}&active=true&closed=false&order=volume24hr&ascending=false"
        )
        return [event_hit(e) for e in data]

    async def read(self, url, *, query=None, section=None, include_comments=True):
        parts = [p for p in urlsplit(url).path.split("/") if p]
        if not parts:  # the front page: trending markets
            return await self.feed("trending")
        if len(parts) < 2 or parts[0] not in ("event", "market"):
            raise WireUnsupported("polymarket: only /event/<slug> URLs and the front page")
        data, f = await self.fetch_json(f"{GAMMA}/events?slug={quote(parts[1])}")
        if not data:
            raise WireUnsupported(f"polymarket: no event {parts[1]!r}")
        e = data[0]
        lines = [f"- {line}" for line in event_summary(e, 20)]
        body = "\n".join([e.get("description") or "", "", "Markets:", *lines]).strip()
        hit = event_hit(e)
        return self.doc(
            f, url, title=e.get("title"), timestamp=ts(e.get("startDate")), body=body,
            metadata={"volume": hit.engagement["volume"], "volume_24h": hit.engagement["volume24h"],
                      "ends": (e.get("endDate") or "")[:10] or None},
        )  # fmt: skip


CHANNEL = PolymarketChannel
