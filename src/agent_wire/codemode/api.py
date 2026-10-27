"""The `wire.*` functions a Code Mode script can call, and the API declaration the model reads."""

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel


class _Opts(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class ReadOpts(_Opts):
    query: str | None = None
    section: str | None = None
    comments: bool = True
    max_tokens: int | None = Field(None, ge=100, le=20_000)


class SearchOpts(_Opts):
    platforms: str | list[str] | None = None
    limit: int = Field(5, ge=1, le=25)
    sort: str = "relevance"


class FeedOpts(_Opts):
    sort: str = "hot"
    limit: int = Field(10, ge=1, le=50)
    max_tokens: int = Field(2000, ge=100, le=20_000)


class ResearchOpts(_Opts):
    days: int = Field(30, ge=1, le=365)
    platforms: list[str] | None = None
    mode: str = "auto"
    depth: str = "default"


class CiteOpts(_Opts):
    quotes: bool = True


STR, STRS = str, (str, list)
_KINDS = {STR: "a string", STRS: "a string or a list of strings"}


def _args(name: str, args: list, positional: tuple[tuple[str, type], ...], opts: type[_Opts]):
    """Split JS call args into checked positionals and an options object."""
    if len(args) > len(positional) + 1:
        raise TypeError(f"wire.{name} takes at most {len(positional) + 1} arguments")
    values = []
    for i, (label, kind) in enumerate(positional):
        v = args[i] if i < len(args) else None
        if not isinstance(v, kind) or (
            isinstance(v, list) and not all(isinstance(x, str) for x in v)
        ):
            raise TypeError(f"wire.{name}: `{label}` must be {_KINDS[kind]}")
        values.append(v)
    raw = args[len(positional)] if len(args) > len(positional) else None
    if raw is not None and not isinstance(raw, dict):
        raise TypeError(f"wire.{name}: options must be an object")
    try:
        return values, opts.model_validate(raw or {})
    except ValidationError as e:
        bad = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise TypeError(f"wire.{name} options: {bad}") from None


async def _read(wire, args):
    (urls,), o = _args("read", args, (("urls", STRS),), ReadOpts)
    return await wire.read_data(urls, o.query, o.section, o.comments, o.max_tokens)


async def _search(wire, args):
    (query,), o = _args("search", args, (("query", STR),), SearchOpts)
    return await wire.search_data(query, o.platforms, o.limit, o.sort)


async def _feed(wire, args):
    (platform, target), o = _args("feed", args, (("platform", STR), ("target", STR)), FeedOpts)
    return await wire.feed_data(platform, target, o.sort, o.limit, o.max_tokens)


async def _research(wire, args):
    (topic,), o = _args("research", args, (("topic", STR),), ResearchOpts)
    r, _, data = await wire.research_result(topic, days=o.days, platforms=o.platforms,
                                            mode=o.mode, depth=o.depth)  # fmt: skip
    return data, sum(len(i.hit.snippet) + len(i.hit.title) for i in r.items) // 4


async def _cite(wire, args):
    (text,), o = _args("cite", args, (("text", STRS),), CiteOpts)
    return await wire.cite_data(text, o.quotes)


BINDINGS = {"read": _read, "search": _search, "feed": _feed, "research": _research, "cite": _cite}
NAMES = tuple(BINDINGS)


async def call(wire, name: str, args: list) -> tuple[object, int]:
    if name not in BINDINGS:
        raise TypeError(f"wire.{name} does not exist")
    return await BINDINGS[name](wire, args)


API = """\
Write the body of an async JS function; `return` the final value. Only that value (plus \
console.log output) comes back to you, so filter, join and trim inside the script. \
Run independent calls together with Promise.all. No fetch, require, fs or timers.
Doc = {url, title, channel, author, date, score, body, comments: {author, score, text, replies}[], error?}
Hit = {url, title, snippet, source, date, author, link, engagement}
wire.read(urls: string|string[] /*max 10*/, {query?, section?, comments?, maxTokens? /*per doc*/}): Promise<Doc[]>
wire.search(query, {platforms?: string|string[] /*web reddit x github youtube hn stackoverflow \
arxiv wikipedia linkedin markets bilibili xhs zhihu all*/, limit?, sort?}): Promise<Hit[]>
wire.feed(platform, target /*'r/rust', '@user', 'trending'*/, {sort?, limit?, maxTokens?}): Promise<Doc>
wire.research(topic, {days?, platforms?, depth?}): Promise<object> // cited last-N-days brief data
wire.cite(text: string|string[], {quotes?}): Promise<{checks: {url, verdict, status, title, \
archive, note, seen}[], unsourced: string[]}>
A failed call rejects its promise (catch it to continue). Max 40 calls per script."""

API_LOCAL = (
    "Async JS body; `return` result. wire.read(urls,{query}) wire.search(q,{platforms}) "
    "wire.feed(platform,target) wire.research(topic) wire.cite(text). Promise.all ok."
)
