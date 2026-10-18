"""Score research items by relevance x (engagement, freshness), with a relevance floor."""

import math
from dataclasses import dataclass
from datetime import datetime

from agent_wire.models import WireSearchHit
from agent_wire.research.planner import terms

# How much one unit of each public signal is worth, roughly "one upvote".
WEIGHTS = {"score": 1.0, "likes": 1.0, "comments": 2.0, "reposts": 2.0, "reactions": 1.0,
           "stars": 0.1, "forks": 0.2, "views": 0.003, "volume24h": 0.001, "volume": 0.0001}  # fmt: skip
RELEVANCE_FLOOR = 0.34  # below this an item is off-topic, however viral
UNDATED_FRESHNESS = 0.35
MIN_SOURCE_TOP = math.log1p(50)


@dataclass
class Item:
    hit: WireSearchHit
    query: str = ""
    origin: str = ""  # the planned source that produced it ("news" for a Techmeme item)
    relevance: float = 0.0
    freshness: float = 0.0
    engagement: float = 0.0
    score: float = 0.0

    @property
    def source(self) -> str:
        return self.hit.source


def raw_engagement(hit: WireSearchHit) -> float:
    return sum(WEIGHTS.get(k, 0.0) * max(0, v) for k, v in hit.engagement.items())


def relevance(hit: WireSearchHit, topic: str) -> float:
    """Share of topic terms present (title counts fully, snippet partly) + exact-phrase bonus."""
    want = list(dict.fromkeys(terms(topic)))
    if not want:
        return 1.0
    title, body = hit.title.lower(), f"{hit.snippet} {hit.author or ''}".lower()
    got = sum(1.0 if t in title else 0.6 if t in body else 0.0 for t in want) / len(want)
    if topic.lower().strip() in title:
        got += 0.25
    return min(got, 1.25)


def freshness(ts: datetime | None, until: datetime, days: int) -> float:
    if ts is None:
        return UNDATED_FRESHNESS
    age = max(0.0, (until - ts).total_seconds() / 86400)
    return 0.5 ** (age / max(days / 2, 0.5))  # half-life: half the window


def in_window(hit: WireSearchHit, since: datetime, until: datetime) -> bool:
    return hit.timestamp is None or since <= hit.timestamp <= until


def floor_for(query: str) -> float:
    """Two-word topics need both words ('rust programming' is not any programming post)."""
    n = len(set(terms(query)))
    return 0.55 if n == 2 else 0.4 if n >= 3 else RELEVANCE_FLOOR


def score_items(items: list[Item], topic: str, since: datetime, until: datetime, days: int,
                floor: float | None = None) -> list[Item]:  # fmt: skip
    """Drop out-of-window and off-topic items; fill the score fields; best first.

    `floor=None` picks a relevance floor from each item's query; pass a number to override.
    """
    kept = [i for i in items if in_window(i.hit, since, until)]
    for i in kept:
        i.relevance = relevance(i.hit, i.query or topic)
    kept = [i for i in kept
            if i.relevance >= (floor if floor is not None else floor_for(i.query or topic))]  # fmt: skip
    logs = {id(i): math.log1p(raw_engagement(i.hit)) for i in kept}
    top_all = max(logs.values(), default=0.0) or 1.0
    top_src: dict[str, float] = {}
    for i in kept:
        top_src[i.source] = max(top_src.get(i.source, 0.0), logs[id(i)])
    for i in kept:
        # per-source normalisation keeps a 50-point HN story from losing to a 5k-view video
        # ... but a source whose best item has ~no engagement must not get a free 1.0
        src_norm = max(top_src[i.source], MIN_SOURCE_TOP)
        i.engagement = 0.6 * logs[id(i)] / src_norm + 0.4 * logs[id(i)] / top_all
        i.freshness = freshness(i.hit.timestamp, until, days)
        i.score = round(min(i.relevance, 1.0) * (0.45 * i.engagement + 0.35 * i.freshness + 0.2), 4)
    return sorted(kept, key=lambda i: -i.score)
