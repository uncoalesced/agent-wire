"""wire_research: fan out across key-free sources inside a time window, score, cluster, brief."""

import asyncio
import inspect
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise

from curl_cffi.requests.exceptions import RequestException

from agent_wire.channels.base import WireUnsupported, relative_ts, ts
from agent_wire.compression.bm25_slicer import rank_comments
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.inbound_shield import shield_comments
from agent_wire.firewall.outbound_privacy import WirePrivacyBlockError, scrub
from agent_wire.models import WireComment, WireSearchHit
from agent_wire.research.cluster import Cluster, cluster
from agent_wire.research.planner import Plan, make_plan, terms
from agent_wire.research.scoring import RELEVANCE_FLOOR, Item, raw_engagement, score_items

SOURCE_CHANNEL = {"reddit": "reddit", "hackernews": "hackernews", "youtube": "youtube",
                  "github": "github", "news": "news", "bluesky": "fediverse",
                  "mastodon": "fediverse", "polymarket": "polymarket", "web": "search",
                  "devto": "devto", "stackoverflow": "stackoverflow", "arxiv": "arxiv",
                  "stocktwits": "stocktwits", "x": "x", "lobsters": "hackernews"}  # fmt: skip
SOURCE_TIMEOUT_S = {"reddit": 60, "web": 30}  # Arctic Shift and engine fan-out are slower
DEFAULT_TIMEOUT_S = 25
EXPECTED = (WireFetchError, WireUnsupported, WirePrivacyBlockError, RequestException, ValueError,
            KeyError, TimeoutError, TypeError)  # fmt: skip
READABLE = frozenset({"hackernews", "reddit", "youtube", "github", "devto", "bluesky", "mastodon",
                      "stackoverflow", "lobsters", "polymarket"})  # fmt: skip
ALGOLIA = "https://hn.algolia.com/api/v1"
COMPARE_FLOOR = 0.2  # "A vs B" items only need to be on-topic for one side


@dataclass
class Take:
    cluster: int
    url: str
    comment: WireComment


@dataclass
class ResearchResult:
    plan: Plan
    clusters: list[Cluster] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # source -> items kept
    gaps: dict[str, str] = field(default_factory=dict)  # source -> why it returned nothing
    takes: list[Take] = field(default_factory=list)
    excerpts: dict[int, str] = field(default_factory=dict)  # cluster -> deep-read excerpt
    subjects: list[dict] = field(default_factory=list)  # compare mode
    topics: list[dict] = field(default_factory=list)  # discover mode
    person: str | None = None
    library: list[dict] = field(default_factory=list)
    markets: list[Item] = field(default_factory=list)  # Polymarket odds, shown on their own
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    elapsed_ms: int = 0
    run_id: int | None = None

    @property
    def items(self) -> list[Item]:
        return [i for c in self.clusters for i in c.items]


def _aware(hit: WireSearchHit) -> WireSearchHit:
    if hit.timestamp and hit.timestamp.tzinfo is None:
        return hit.model_copy(update={"timestamp": hit.timestamp.replace(tzinfo=UTC)})
    return hit


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}  # fmt: skip
_DMY = re.compile(r"^\s*(\d{1,2}) ([A-Za-z]{3})[a-z]* (\d{4})")
_MDY = re.compile(r"^\s*([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})")


def snippet_date(snippet: str) -> datetime | None:
    """Search engines prefix snippets with '2 days ago -' or '16 April 2026 -' / 'Apr 16, 2026 -'."""
    head = snippet[:40]
    if " ago" in head:
        return relative_ts(head)
    for rx, order in ((_DMY, (0, 1, 2)), (_MDY, (1, 0, 2))):
        if m := rx.match(head):
            day, mon, year = (m.groups()[i] for i in order)
            if month := _MONTHS.get(mon.lower()):
                try:
                    return datetime(int(year), month, int(day), tzinfo=UTC)
                except ValueError:
                    return None
    return None


def _accepts_since(fn) -> bool:
    try:
        return "since" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def mentions(item: Item, subjects: list[str]) -> list[str]:
    text = f"{item.hit.title} {item.hit.snippet}".lower()
    return [s for s in subjects if all(t in text for t in terms(s))]


class Researcher:
    def __init__(self, wire):
        self.wire = wire
        self.ctx = wire.ctx

    # ---- collection --------------------------------------------------------------------------
    async def _search(self, source: str, query: str, plan: Plan, limit: int) -> list[WireSearchHit]:
        ch = self.ctx.channel(SOURCE_CHANNEL[source])
        if source == "mastodon":
            words = terms(query)
            return await ch.mastodon_tag(max(words, key=len), limit=limit) if words else []
        if source == "lobsters":
            return await self._lobsters(query, limit)
        kw = {"limit": limit}
        if _accepts_since(ch.search):
            kw["since"] = plan.since
        return await ch.search(query, **kw)

    async def _lobsters(self, query: str, limit: int) -> list[WireSearchHit]:
        """Lobsters search is bot-walled; its hottest JSON is open, so filter that."""
        data, _ = await self.ctx.channel("hackernews").fetch_json("https://lobste.rs/hottest.json")
        want = set(terms(query))
        hits = []
        for s in data:
            text = f"{s.get('title', '')} {' '.join(s.get('tags') or [])}".lower()
            if want and not any(w in text for w in want):
                continue
            hits.append(WireSearchHit(
                url=s.get("short_id_url") or s.get("comments_url") or "", title=s.get("title") or "",
                snippet=", ".join(s.get("tags") or []), source="lobsters", link=s.get("url") or None,
                timestamp=ts(s.get("created_at")), rank_score=float(s.get("score") or 0),
                engagement={"score": s.get("score") or 0, "comments": s.get("comment_count") or 0},
            ))  # fmt: skip
        return hits[:limit]

    async def _collect(self, plan: Plan, limit: int, result: ResearchResult) -> list[Item]:
        async def one(source: str, q: str):
            timeout = SOURCE_TIMEOUT_S.get(source, DEFAULT_TIMEOUT_S)
            try:
                hits = await asyncio.wait_for(self._search(source, q, plan, limit), timeout)
                return source, q, hits, None
            except EXPECTED as e:
                return source, q, [], f"{type(e).__name__}: {scrub(str(e))[:160]}"

        jobs = [one(s, q) for s in plan.sources for q in plan.queries.get(s) or []]
        items, ok = [], set()
        for source, q, hits, err in await asyncio.gather(*jobs):
            if err:
                result.gaps.setdefault(source, err)
                continue
            ok.add(source)
            for h in hits:
                h = _aware(h)
                if h.timestamp is None and source == "web":
                    h = h.model_copy(update={"timestamp": snippet_date(h.snippet)})
                if h.source != source and source not in ("news", "arxiv"):
                    h = h.model_copy(update={"source": source})  # web engines, bsky -> source name
                items.append(Item(hit=h, query=q, origin=source))
        for s in ok:
            result.gaps.pop(s, None)
        return items

    # ---- modes -------------------------------------------------------------------------------
    async def run(self, topic: str = "", *, days: int = 30, sources=None, mode: str = "auto",
                  depth: str = "default", as_of=None, github_user: str | None = None,
                  library=None) -> ResearchResult:  # fmt: skip
        t0 = time.perf_counter()
        has_x = bool(self.ctx.vault and self.ctx.vault.get("x.com"))
        plan = make_plan(topic, days=days, sources=sources, mode=mode, depth=depth, as_of=as_of,
                         github_user=github_user, has_x_session=has_x)  # fmt: skip
        result = ResearchResult(plan=plan)
        if plan.mode == "discover":
            await self._discover(plan, result)
        elif plan.mode == "person":
            await self._person(plan, result)
        else:
            await self._topic(plan, result)
            if plan.github_user:
                await self._person(plan, result)
        if library is not None and plan.topic:
            result.library = library.related(plan.topic, limit=3)
        result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return result

    async def _topic(self, plan: Plan, result: ResearchResult) -> None:
        items = await self._collect(plan, plan.per_source, result)
        compare = plan.mode == "compare"
        if compare:
            result.subjects = self._compare(plan, items)
        floor = COMPARE_FLOOR if compare else None
        scored = score_items(items, plan.topic, plan.since, plan.until, plan.days, floor=floor)
        if compare:  # keep items that are clearly about at least one side
            scored = [
                i for i in scored if i.relevance >= RELEVANCE_FLOOR or mentions(i, plan.subjects)
            ]
        result.counts = dict(Counter(i.source for i in scored))
        answered = {i.origin for i in scored}
        for s in plan.sources:
            if s not in answered and s not in result.gaps:
                result.gaps[s] = "answered, but nothing on-topic inside the window"
        result.markets = [i for i in scored if i.source == "polymarket"]
        result.clusters = cluster([i for i in scored if i.source != "polymarket"])
        await self._deep_read(plan, result)

    def _compare(self, plan: Plan, items: list[Item]) -> list[dict]:
        out = []
        for subj in plan.subjects:
            pool = [Item(hit=i.hit, query=subj) for i in items
                    if i.query == subj or mentions(i, [subj])]  # fmt: skip
            mine = score_items(pool, subj, plan.since, plan.until, plan.days)
            eng: Counter = Counter()
            for i in mine:
                eng.update(i.hit.engagement)
            top = mine[0].hit if mine else None
            out.append({
                "subject": subj, "items": len(mine), "sources": sorted({i.source for i in mine}),
                "engagement": dict(eng), "heat": round(sum(raw_engagement(i.hit) for i in mine)),
                "top": {"title": top.title, "url": top.url, "source": top.source} if top else None,
                "head_to_head": sum(1 for i in mine if len(mentions(i, plan.subjects)) > 1),
            })  # fmt: skip
        return out

    async def _deep_read(self, plan: Plan, result: ResearchResult) -> None:
        """'Best takes': read the top clusters and keep their strongest on-topic comments."""
        picks = []
        for idx, c in enumerate(result.clusters):
            if len(picks) >= plan.deep_reads:
                break
            ranked = sorted(c.items, key=lambda i: -i.score)
            lead = next((i for i in ranked if i.source in READABLE), None)
            if lead:
                picks.append((idx, lead.hit.url))
        if not picks:
            return
        reads = await asyncio.gather(
            *(
                asyncio.wait_for(self.wire._read_one(u, plan.topic, None, True), 40)
                for _, u in picks
            ),
            return_exceptions=True,
        )
        for (idx, url), r in zip(picks, reads, strict=True):
            if isinstance(r, BaseException):
                if not isinstance(r, EXPECTED):
                    raise r
                continue
            doc, _ = r
            if body := " ".join(scrub(doc.body).split()):
                result.excerpts[idx] = body[:400]
            best = rank_comments(shield_comments(doc.comments), plan.topic, 400)
            for c in sorted(best, key=lambda c: -c.score)[:2]:
                flat = c.model_copy(update={"replies": [], "body": scrub(c.body)})
                result.takes.append(Take(idx, url, flat))

    async def _person(self, plan: Plan, result: ResearchResult) -> None:
        try:
            doc = await self.ctx.channel("github").person(plan.github_user, since=plan.since)
            result.person = scrub(doc.body)
        except EXPECTED as e:
            result.gaps["github-person"] = f"{type(e).__name__}: {scrub(str(e))[:160]}"

    async def _discover(self, plan: Plan, result: ResearchResult) -> None:
        """Topic-less: what is rising across sources in the window, with momentum labels."""
        hn, gh, news = (self.ctx.channel(n) for n in ("hackernews", "github", "news"))
        day, since_s = plan.since.date().isoformat(), int(plan.since.timestamp())

        async def hn_top():
            data, _ = await hn.fetch_json(f"{ALGOLIA}/search?tags=story&hitsPerPage=40"
                                          f"&numericFilters=created_at_i>{since_s},points>50")  # fmt: skip
            return [WireSearchHit(url=f"https://news.ycombinator.com/item?id={h['objectID']}",
                                  title=h.get("title") or "", source="hackernews", link=h.get("url"),
                                  timestamp=ts(h.get("created_at_i")),
                                  engagement={"score": h.get("points") or 0,
                                              "comments": h.get("num_comments") or 0})
                    for h in data.get("hits", [])]  # fmt: skip

        async def mastodon_trends():
            data, _ = await hn.fetch_json("https://mastodon.social/api/v1/trends/tags?limit=10")
            return [WireSearchHit(url=t.get("url") or "", title=f"#{t.get('name')}", source="mastodon",
                                  engagement={"comments": sum(int(d.get("uses", 0))
                                                              for d in t.get("history") or [])})
                    for t in data]  # fmt: skip

        makers = {
            "hackernews": hn_top,
            "github": lambda: gh.search(f"created:>{day} stars:>20", limit=15, sort="stars"),
            "news": lambda: news.techmeme(since=plan.since),
            "polymarket": lambda: self.ctx.channel("polymarket").trending_hits(10),
            "mastodon": mastodon_trends,
            "lobsters": lambda: self._lobsters("", 25),
        }
        for s in plan.sources:
            if s not in makers:
                result.gaps[s] = "not used in discover mode"
        names = [s for s in plan.sources if s in makers]
        done = await asyncio.gather(*(asyncio.wait_for(makers[s](), DEFAULT_TIMEOUT_S) for s in names),
                                    return_exceptions=True)  # fmt: skip
        items = []
        for source, r in zip(names, done, strict=True):
            if isinstance(r, BaseException):
                if not isinstance(r, EXPECTED):
                    raise r
                result.gaps[source] = f"{type(r).__name__}: {scrub(str(r))[:160]}"
                continue
            items += [Item(hit=_aware(h).model_copy(update={"source": source})) for h in r]
        scored = score_items(items, "", plan.since, plan.until, plan.days, floor=0.0)
        result.counts = dict(Counter(i.source for i in scored))
        result.markets = [i for i in scored if i.source == "polymarket"]
        result.clusters = cluster([i for i in scored if i.source != "polymarket"])
        result.topics = rising_topics(result.clusters, plan.until)


_TOPIC_STOP = frozenset(
    ["the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "by", "at", "from", "is", "are", "was", "be", "it", "its", "this", "that", "how", "why", "what", "show", "ask", "hn", "new", "you", "your", "we", "our", "my", "i", "can", "will", "not", "now", "just", "over", "after", "into", "about", "more", "than", "first", "one", "two", "us", "vs", "via", "using", "use", "get", "has", "have", "make", "made", "says", "said", "year", "years", "day", "days", "week", "launches", "launched", "introducing", "announces", "released", "release", "open", "source"]
)  # fmt: skip


_COMMON = frozenset(
    [
        "about",
        "above",
        "across",
        "after",
        "again",
        "against",
        "all",
        "almost",
        "also",
        "always",
        "among",
        "another",
        "any",
        "anyone",
        "anything",
        "around",
        "away",
        "back",
        "bad",
        "because",
        "been",
        "before",
        "behind",
        "being",
        "best",
        "better",
        "between",
        "big",
        "both",
        "but",
        "came",
        "cannot",
        "case",
        "change",
        "come",
        "could",
        "did",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "early",
        "even",
        "ever",
        "every",
        "few",
        "find",
        "for",
        "found",
        "free",
        "from",
        "full",
        "gets",
        "give",
        "given",
        "going",
        "gone",
        "good",
        "great",
        "had",
        "half",
        "hand",
        "hard",
        "has",
        "have",
        "help",
        "her",
        "here",
        "high",
        "him",
        "his",
        "home",
        "how",
        "however",
        "into",
        "its",
        "itself",
        "just",
        "keep",
        "kind",
        "know",
        "large",
        "last",
        "later",
        "least",
        "less",
        "let",
        "life",
        "like",
        "little",
        "long",
        "look",
        "lost",
        "made",
        "make",
        "making",
        "many",
        "may",
        "maybe",
        "might",
        "more",
        "most",
        "much",
        "must",
        "need",
        "never",
        "next",
        "nice",
        "nothing",
        "now",
        "off",
        "often",
        "old",
        "once",
        "only",
        "other",
        "our",
        "out",
        "over",
        "own",
        "part",
        "people",
        "place",
        "point",
        "put",
        "real",
        "really",
        "right",
        "run",
        "said",
        "same",
        "say",
        "see",
        "seems",
        "should",
        "show",
        "side",
        "since",
        "small",
        "some",
        "something",
        "still",
        "such",
        "sure",
        "take",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "think",
        "this",
        "those",
        "though",
        "through",
        "time",
        "today",
        "together",
        "too",
        "took",
        "top",
        "toward",
        "try",
        "two",
        "under",
        "until",
        "upon",
        "use",
        "used",
        "very",
        "want",
        "was",
        "way",
        "ways",
        "well",
        "went",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whole",
        "why",
        "will",
        "with",
        "within",
        "without",
        "work",
        "works",
        "world",
        "would",
        "wrong",
        "year",
        "years",
        "yet",
        "you",
        "your",
        "why",
        "what's",
        "it's",
        "i'm",
        "don't",
        "doesn't",
        "can't",
        "won't",
        "isn't",
        "vs",
        "via",
        "new",
        "now",
        "how",
        "ask",
        "show",
        "tell",
        "hn",
        "launch",
        "launches",
        "launched",
        "introducing",
        "announces",
        "released",
        "release",
        "open",
        "source",
        "state",
        "data",
        "says",
        "first",
        "one",
        "report",
        "reports",
        "amid",
        "series",
    ]
)


def entities(title: str) -> set[str]:
    """Named things in a headline: Capitalised / MixedCase / ACRONYM / v2-style tokens and pairs."""
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.'\-]*[A-Za-z0-9+#]|[A-Za-z0-9]", title)
    named = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in _COMMON or low in _TOPIC_STOP or len(tok) < 3 or not any(map(str.isalpha, tok)):
            named.append(None)
            continue
        is_named = tok[0].isupper() and (i > 0 or not tok[1:].islower() or len(tokens) > 1)
        is_named = is_named or any(ch.isdigit() for ch in tok) or tok.isupper()
        named.append(low if is_named else None)
    out = {t for t in named if t}
    out |= {f"{a} {b}" for a, b in pairwise(named) if a and b}
    return out


def rising_topics(clusters: list[Cluster], now: datetime, n: int = 10) -> list[dict]:
    """Rank recurring title words/phrases by cross-source spread and engagement."""
    weight: Counter = Counter()
    where: dict[str, set] = {}
    examples: dict[str, list[Cluster]] = {}
    for c in clusters:
        if c.sources == ["polymarket"]:
            continue  # trending markets are mostly tonight's games; they get their own list
        for g in entities(c.title):
            weight[g] += c.score
            where.setdefault(g, set()).update(c.sources)
            examples.setdefault(g, []).append(c)

    def rank(g: str) -> float:
        return weight[g] * (1 + 0.5 * (len(where[g]) - 1)) * (1.2 if " " in g else 1.0)

    out, used, stories = [], [], []
    for g in sorted(weight, key=lambda g: -rank(g)):
        if len(examples[g]) < 2:
            continue  # a phrase from one story is that story (already in "Biggest stories")
        if any(g in u or u in g for u in used):
            continue
        mine = {id(c) for c in examples[g]}
        same = next((i for i, s in enumerate(stories) if len(mine & s) * 3 >= len(mine) * 2), None)
        if same is not None:
            if out[same]["topic"].count(" / ") < 2:  # "gemini" + "argon": the same stories
                out[same]["topic"] += f" / {g}"
            used.append(g)
            continue
        used.append(g)
        stories.append(mine)
        ex = sorted(examples[g], key=lambda c: -c.score)[:3]
        fresh = sum(1 for c in examples[g] if c.momentum(now) in ("new", "breaking"))
        label = ("rising" if fresh * 2 >= len(examples[g])
                 else "hot" if len(where[g]) >= 3 else "steady")  # fmt: skip
        out.append({"topic": g, "mentions": len(examples[g]), "sources": sorted(where[g]),
                    "momentum": label,
                    "examples": [{"title": c.title, "url": c.lead.hit.url} for c in ex]})  # fmt: skip
        if len(out) >= n:
            break
    return out
