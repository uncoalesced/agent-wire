"""Pre-research: decide mode, window, sources and per-source queries before any network call."""

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

# Key-free sources researched by default; others join when the topic calls for them.
CORE = (
    "reddit",
    "hackernews",
    "youtube",
    "github",
    "news",
    "bluesky",
    "mastodon",
    "polymarket",
    "web",
)
TECH = ("devto", "stackoverflow")
ALL_SOURCES = (*CORE, *TECH, "arxiv", "stocktwits", "x", "lobsters")
ALIASES = {"hn": "hackernews", "gh": "github", "yt": "youtube", "so": "stackoverflow",
           "bsky": "bluesky", "fediverse": "bluesky", "twitter": "x", "techmeme": "news",
           "papers": "arxiv", "dev.to": "devto", "markets": "polymarket"}  # fmt: skip
DEPTHS = {"quick": {"per_source": 5, "deep_reads": 0, "queries": 1},
          "default": {"per_source": 8, "deep_reads": 3, "queries": 1},
          "deep": {"per_source": 15, "deep_reads": 6, "queries": 2}}  # fmt: skip

_TECH_WORDS = frozenset(
    ["ai", "llm", "gpt", "model", "api", "sdk", "python", "rust", "go", "golang", "typescript", "javascript", "js", "react", "vue", "svelte", "node", "deno", "bun", "docker", "kubernetes", "k8s", "linux", "git", "github", "cli", "framework", "library", "database", "sql", "postgres", "redis", "cloud", "aws", "gcp", "azure", "devops", "ml", "agent", "agents", "mcp", "claude", "openai", "anthropic", "gemini", "llama", "compiler", "wasm", "webassembly", "kernel", "gpu", "cuda", "nvidia", "programming", "code", "coding", "developer", "open-source", "opensource", "vscode", "cursor", "neovim", "vim", "terminal", "uv", "pip", "npm", "cargo", "crate", "package"]
)  # fmt: skip
_ACADEMIC = frozenset(
    [
        "paper",
        "papers",
        "arxiv",
        "research",
        "study",
        "benchmark",
        "dataset",
        "theorem",
        "diffusion",
        "transformer",
    ]
)
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "with",
        "what",
        "is",
        "are",
        "how",
        "why",
        "best",
        "new",
        "about",
        "latest",
        "news",
        "last",
        "days",
        "week",
        "month",
        "people",
        "think",
    ]
)
_VS = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)
_TICKER = re.compile(r"\$([A-Za-z]{1,6}(?:\.[A-Za-z]{1,2})?)\b")
_DISCOVER = re.compile(
    r"^\s*(?:what'?s\s+)?(?:trending|hot|discover|what is happening)\b", re.IGNORECASE
)


@dataclass
class Plan:
    topic: str
    mode: str  # topic | compare | discover | person
    days: int
    since: datetime
    until: datetime
    depth: str
    sources: list[str]
    queries: dict[str, list[str]] = field(default_factory=dict)
    subjects: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    github_user: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def per_source(self) -> int:
        return DEPTHS[self.depth]["per_source"]

    @property
    def deep_reads(self) -> int:
        return DEPTHS[self.depth]["deep_reads"]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["since"], d["until"] = self.since.isoformat(), self.until.isoformat()
        return d

    def describe(self) -> str:
        """--preflight: what would run, without running it."""
        window = f"{self.since.date()} -> {self.until.date()} ({self.days} days)"
        lines = [f"mode: {self.mode} · depth: {self.depth} · window: {window}"]
        if self.subjects:
            lines.append("subjects: " + " | ".join(self.subjects))
        if self.github_user:
            lines.append(f"github person: {self.github_user}")
        lines.append("reads (anonymous unless you saved a session with `wire auth login`):")
        lines += [f"- {s}: " + " · ".join(repr(q) for q in self.queries.get(s) or [self.topic])
                  for s in self.sources]  # fmt: skip
        lines.append("writes: none unless store=True (then WIRE_HOME/research.db)")
        lines += [f"note: {n}" for n in self.notes]
        return "\n".join(lines)


def terms(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+#.\-]*", text.lower())
    return [w.strip(".-") for w in words if w not in _STOP and len(w) > 1]


def is_tech(topic: str) -> bool:
    return any(t in _TECH_WORDS for t in terms(topic))


def _expand(topic: str, n: int) -> list[str]:
    """Query expansion: the topic, then (deep) its key terms without filler words."""
    out = [topic.strip()]
    core = " ".join(terms(topic))
    if n > 1 and core and core != topic.strip().lower():
        out.append(core)
    return out[:n]


def normalize_sources(sources) -> list[str] | None:
    if not sources:
        return None
    if isinstance(sources, str):
        sources = [s for s in re.split(r"[,\s]+", sources) if s]
    out = []
    for s in sources:
        key = ALIASES.get(s.strip().lower(), s.strip().lower())
        if key == "all":
            return list(ALL_SOURCES)
        if key not in ALL_SOURCES:
            raise ValueError(
                f"unknown research source {s!r}; choose from: {', '.join(ALL_SOURCES)}"
            )
        out.append(key)
    return list(dict.fromkeys(out))


def make_plan(topic: str = "", *, days: int = 30, sources=None, mode: str = "auto",
              depth: str = "default", as_of=None, github_user: str | None = None,
              has_x_session: bool = False) -> Plan:  # fmt: skip
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(DEPTHS)}")
    days = max(1, min(int(days), 365))
    until = _as_of(as_of)
    since = until - timedelta(days=days)
    topic = (topic or "").strip()
    chosen = normalize_sources(sources)

    if mode == "auto":
        if github_user and not topic:
            mode = "person"
        elif not topic or _DISCOVER.match(topic):
            mode = "discover"
        elif len(_VS.split(topic)) > 1:
            mode = "compare"
        else:
            mode = "topic"
    if mode not in ("topic", "compare", "discover", "person"):
        raise ValueError("mode must be auto, topic, compare, discover or person")

    plan = Plan(topic=topic, mode=mode, days=days, since=since, until=until, depth=depth,
                sources=[], github_user=github_user.lstrip("@") if github_user else None)  # fmt: skip
    plan.tickers = [t.upper() for t in _TICKER.findall(topic)]
    if mode == "compare":
        plan.subjects = [s.strip() for s in _VS.split(topic) if s.strip()][:4]

    if chosen:
        plan.sources = chosen
    elif mode == "discover":
        plan.sources = ["hackernews", "github", "news", "polymarket", "mastodon", "lobsters"]
    elif mode == "person":
        plan.sources = ["github"]
    else:
        plan.sources = list(CORE)
        if is_tech(topic):
            plan.sources += list(TECH)
        if set(terms(topic)) & _ACADEMIC:
            plan.sources.append("arxiv")
        if plan.tickers:
            plan.sources.append("stocktwits")
    if "x" in plan.sources and not has_x_session:
        plan.sources.remove("x")
        plan.notes.append("x skipped: no saved session (`wire auth login x.com` adds it)")
    elif has_x_session and "x" not in plan.sources and not chosen and mode in ("topic", "compare"):
        plan.sources.append("x")

    n = DEPTHS[depth]["queries"]
    subjects = plan.subjects or ([topic] if topic else [])
    for s in plan.sources:
        if s == "stocktwits":
            plan.queries[s] = [f"${t}" for t in plan.tickers] or subjects
        else:
            plan.queries[s] = [q for subj in subjects for q in _expand(subj, n)]
    if plan.github_user and "github" not in plan.sources:
        plan.sources.append("github")
    return plan


def _as_of(value) -> datetime:
    if value in (None, ""):
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    d = datetime.fromisoformat(str(value))
    if len(str(value)) <= 10:  # a bare date means the end of that day
        d += timedelta(days=1)
    return d if d.tzinfo else d.replace(tzinfo=UTC)
