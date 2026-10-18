"""Cross-source dedup: one story told on HN, Reddit and Techmeme becomes one cluster."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agent_wire.channels.tier1_global.search import url_key
from agent_wire.research.scoring import Item, raw_engagement

TITLE_JACCARD = 0.6
_WORD = re.compile(r"[a-z0-9]+")
_NOISE = frozenset(
    [
        "show",
        "ask",
        "hn",
        "tell",
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "is",
        "are",
        "with",
        "by",
        "at",
        "from",
    ]
)


@dataclass
class Cluster:
    items: list[Item] = field(default_factory=list)

    @property
    def lead(self) -> Item:
        return max(self.items, key=lambda i: i.score)

    @property
    def title(self) -> str:
        return self.lead.hit.title

    @property
    def sources(self) -> list[str]:
        return sorted({i.source for i in self.items})

    @property
    def score(self) -> float:
        # agreement across platforms is the strongest signal there is
        return round(self.lead.score * (1 + 0.25 * (len(self.sources) - 1)), 4)

    @property
    def engagement(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for i in self.items:
            for k, v in i.hit.engagement.items():
                total[k] = total.get(k, 0) + v
        return total

    @property
    def first_seen(self) -> datetime | None:
        dates = [i.hit.timestamp for i in self.items if i.hit.timestamp]
        return min(dates) if dates else None

    @property
    def link(self) -> str | None:
        return next((i.hit.link for i in self.items if i.hit.link), None)

    def momentum(self, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        first = self.first_seen
        heat = sum(raw_engagement(i.hit) for i in self.items)
        if first and now - first < timedelta(days=3):
            return "breaking" if heat > 200 or len(self.sources) > 1 else "new"
        if len(self.sources) >= 3 or heat > 2000:
            return "hot"
        return "steady"


def _title_words(title: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(title.lower()) if w not in _NOISE)


def _jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def cluster(items: list[Item]) -> list[Cluster]:
    """Union-find over shared URL, shared outbound link, or near-identical titles."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        parent[find(a)] = find(b)

    by_key: dict[str, int] = {}
    for idx, it in enumerate(items):
        for u in (it.hit.url, it.hit.link):
            if not u:
                continue
            key = url_key(u)
            if key in by_key:
                union(idx, by_key[key])
            else:
                by_key[key] = idx
    words = [_title_words(i.hit.title) for i in items]
    for a in range(len(items)):
        if len(words[a]) < 4:
            continue  # short titles ("@user", "ZED and python") collide by accident
        for b in range(a):
            if len(words[b]) >= 4 and _jaccard(words[a], words[b]) >= TITLE_JACCARD:
                union(a, b)

    groups: dict[int, Cluster] = {}
    for idx, it in enumerate(items):
        groups.setdefault(find(idx), Cluster()).items.append(it)
    return sorted(groups.values(), key=lambda c: -c.score)
