import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeWaterfall, fetched
from fastmcp import Client

from agent_wire.channels.base import Context, relative_ts
from agent_wire.models import WireSearchHit
from agent_wire.research import brief as briefmod
from agent_wire.research.cluster import cluster
from agent_wire.research.engine import Researcher, entities, rising_topics, snippet_date
from agent_wire.research.planner import make_plan, normalize_sources
from agent_wire.research.scoring import Item, floor_for, freshness, relevance, score_items
from agent_wire.research.store import Store
from agent_wire.research.watch import check_webhook, run_watches
from agent_wire.server import Wire, create_server

NOW = datetime.now(UTC)


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


def hit(title, url, source="hackernews", when=None, link=None, snippet="", **eng):
    return WireSearchHit(url=url, title=title, source=source, timestamp=when, link=link,
                         snippet=snippet, engagement=eng)  # fmt: skip


def run(coro):
    return asyncio.run(coro)


# ---- planner ------------------------------------------------------------------------------------


def test_plan_modes_and_sources():
    p = make_plan("uv python")
    assert (
        p.mode == "topic" and p.days == 30 and "devto" in p.sources
    )  # tech topic adds dev sources
    assert (p.until - p.since).days == 30 and "x" not in p.sources
    assert make_plan("").mode == "discover" and make_plan("what's trending").mode == "discover"
    c = make_plan("Bun vs Deno vs Node")
    assert c.mode == "compare" and c.subjects == ["Bun", "Deno", "Node"]
    assert c.queries["hackernews"] == ["Bun", "Deno", "Node"]
    t = make_plan("$NVDA earnings")
    assert t.tickers == ["NVDA"] and t.queries["stocktwits"] == ["$NVDA"]
    assert make_plan("", github_user="@torvalds").mode == "person"
    assert "arxiv" in make_plan("diffusion transformer paper").sources


def test_plan_window_as_of_and_depth():
    p = make_plan("x", days=7, as_of="2026-01-10", depth="deep")
    assert p.until == datetime(2026, 1, 11, tzinfo=UTC)
    assert p.since == datetime(2026, 1, 4, tzinfo=UTC)
    assert p.per_source == 15 and p.deep_reads == 6
    assert make_plan("x", days=9999).days == 365
    with pytest.raises(ValueError):
        make_plan("x", depth="huge")


def test_plan_sources_aliases_and_x_needs_session():
    assert normalize_sources("hn, gh yt") == ["hackernews", "github", "youtube"]
    with pytest.raises(ValueError, match="unknown research source"):
        normalize_sources(["myspace"])
    p = make_plan("ai agents", sources=["x", "hn"])
    assert p.sources == ["hackernews"] and any("wire auth login x.com" in n for n in p.notes)
    assert "x" in make_plan("ai agents", has_x_session=True).sources
    text = p.describe()
    assert "hackernews: 'ai agents'" in text and "writes: none" in text


def test_query_expansion_in_deep_mode():
    p = make_plan("what is the best python web framework", depth="deep")
    assert p.queries["reddit"] == ["what is the best python web framework", "python web framework"]


# ---- scoring / clustering -----------------------------------------------------------------------


def test_relevance_freshness_and_floors():
    assert relevance(hit("Rust 2.0 released", "u"), "rust") > 1
    assert relevance(hit("Python news", "u", snippet="about rust"), "rust") == pytest.approx(0.6)
    assert relevance(hit("ASMR programming", "u"), "rust programming") < floor_for(
        "rust programming"
    )
    assert freshness(NOW, NOW, 30) == 1
    assert freshness(ago(days=15), NOW, 30) == pytest.approx(0.5)
    assert freshness(None, NOW, 30) == 0.35


def test_score_items_window_floor_and_small_source_damping():
    items = [
        Item(hit("rust async book", "a", when=ago(days=2), score=500, comments=200)),
        Item(hit("rust async patterns", "b", source="reddit", when=ago(days=1), score=2)),
        Item(hit("rust async old", "c", when=ago(days=90), score=9999)),  # outside window
        Item(hit("cooking pasta", "d", when=ago(days=1), score=9999)),  # off topic
    ]
    out = score_items(items, "rust async", ago(days=30), NOW, 30)
    assert [i.hit.url for i in out] == ["a", "b"]
    assert out[1].engagement < 0.3  # a 2-point archive post does not become reddit's "1.0"


def test_cluster_merges_by_url_link_and_title():
    rows = [
        (hit("Show HN: Wire, agents on the web", "https://news.ycombinator.com/item?id=1",
             link="https://wire.dev/post"), 0.9),
        (hit("Wire: agents on the web", "https://www.reddit.com/r/x/1", source="reddit",
             link="https://wire.dev/post/"), 0.5),
        (hit("Totally different story about databases", "https://x.example/2"), 0.3),
        (hit("@bob", "https://bsky.app/1", source="bluesky"), 0.2),
        (hit("@bob", "https://bsky.app/2", source="bluesky"), 0.1),
    ]  # fmt: skip
    items = [Item(h.model_copy(update={"timestamp": ago(hours=5)}), score=s) for h, s in rows]
    cs = cluster(items)
    top = cs[0]
    assert top.sources == ["hackernews", "reddit"] and top.score == pytest.approx(0.9 * 1.25)
    assert top.link == "https://wire.dev/post" and top.momentum(NOW) == "breaking"
    assert len(cs) == 4  # short "@bob" titles are not merged by title


def test_cluster_title_similarity():
    a = Item(hit("Anthropic releases new coding model for agents", "https://a/1"), score=0.5)
    b = Item(hit("Anthropic releases new coding model for agents today", "https://b/2",
                 source="news"), score=0.4)  # fmt: skip
    assert len(cluster([a, b])) == 1


def test_snippet_dates_and_relative_ts():
    assert snippet_date("2 days ago - uv is fast").date() == ago(days=2).date()
    assert snippet_date("16 April 2026 - guide") == datetime(2026, 4, 16, tzinfo=UTC)
    assert snippet_date("Apr 3, 2026 — x") == datetime(2026, 4, 3, tzinfo=UTC)
    assert snippet_date("no date here") is None
    assert relative_ts("Streamed 3 weeks ago", NOW) == NOW - timedelta(weeks=3)


def test_entities_and_rising_topics():
    assert "opencode" in entities("OpenCode Hacked: User credentials leaked")
    assert "all" not in entities("Why are we even doing all this?")
    rows = [("OpenCode 2.0 ships", "hackernews"), ("OpenCode breach hit users", "news"),
            ("Passkeys are a trap", "hackernews"), ("Notre Dame vs. BYU", "polymarket"),
            ("Notre Dame vs. USC", "polymarket")]  # fmt: skip
    items = [Item(hit(t, f"https://e/{n}", source=s, when=ago(hours=6), score=100), score=0.5)
             for n, (t, s) in enumerate(rows)]  # fmt: skip
    topics = rising_topics(cluster(items), NOW)
    assert topics[0]["topic"] == "opencode" and topics[0]["momentum"] == "rising"
    assert not any("notre" in t["topic"] for t in topics)  # sports markets don't make topics


# ---- engine (offline, faked network) ------------------------------------------------------------


def make_wire(tmp_path, routes):
    w = Wire(home=tmp_path)
    w.ctx = Context(pool=None, waterfall=FakeWaterfall(routes), vault=w.vault, ledger=w.ledger)
    return w


def _rfc822(d: datetime) -> str:
    return d.strftime("%a, %d %b %Y %H:%M:%S GMT")


TECHMEME = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Techmeme</title>
<item><title>Wire launches agent web access (The Verge)</title><link>https://www.techmeme.com/1</link>
<pubDate>{_rfc822(NOW - timedelta(hours=3))}</pubDate>
<description>&lt;a href="https://wire.dev/post"&gt;Wire&lt;/a&gt; agent web access</description></item>
</channel></rss>"""
GNEWS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title></channel></rss>"""


def routes():
    stamp = int((NOW - timedelta(hours=5)).timestamp())
    return {
        "hn.algolia.com/api/v1/search": {"hits": [
            {"objectID": "1", "title": "Wire: agent web access", "points": 300, "num_comments": 80,
             "url": "https://wire.dev/post", "created_at_i": stamp, "author": "op"},
            {"objectID": "2", "title": "Unrelated pasta recipe", "points": 999, "num_comments": 1,
             "created_at_i": stamp}]},
        "api.github.com/search/repositories": {"items": [
            {"html_url": "https://github.com/u/wire", "full_name": "u/wire",
             "description": "agent web access", "stargazers_count": 1200,
             "pushed_at": (NOW - timedelta(days=1)).isoformat(), "owner": {"login": "u"}}]},
        "api.github.com/search/issues": {"items": []},
        "techmeme.com/feed.xml": fetched(TECHMEME, ctype="text/xml"),
        "news.google.com": fetched(GNEWS, ctype="application/xml"),
    }  # fmt: skip


def test_research_topic_end_to_end(tmp_path):
    w = make_wire(tmp_path, routes())
    r = run(Researcher(w).run("agent web access", sources=["hn", "github", "news"], depth="quick"))
    assert r.counts == {"hackernews": 1, "github": 1, "techmeme": 1}  # pasta: off-topic
    assert not r.gaps
    top = r.clusters[0]
    assert set(top.sources) == {"hackernews", "techmeme"}  # same article via outbound link
    md = briefmod.to_markdown(r)
    assert "## Top stories" in md and "agent web access · last 30 days" in md
    assert "(The Verge)" not in md  # outlet suffix stripped from Techmeme titles
    data = json.loads(briefmod.to_json(r))
    assert data["mode"] == "topic" and data["clusters"][0]["items"][0]["timestamp"]
    run(w.close())


def test_research_reports_gaps(tmp_path):
    w = make_wire(tmp_path, routes())
    r = run(Researcher(w).run("agent web access", sources=["hn", "devto"], depth="quick"))
    assert "devto" in r.gaps and "WireFetchError" in r.gaps["devto"]
    assert "## Not covered" in briefmod.to_markdown(r)
    run(w.close())


def test_html_brief_escapes_untrusted_titles():
    md = "# t\n1. **[<script>x</script>](javascript:alert(1))** [ok](https://a.b/c)"
    page = briefmod.page(md, "t")
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert 'href="javascript' not in page and 'href="https://a.b/c"' in page


def test_wire_research_store_library_and_preflight(tmp_path):
    w = make_wire(tmp_path, routes())
    pre = run(w.research("agent web access", preflight=True))
    assert pre.startswith("[WIRE | research preflight]") and not w.ctx.waterfall.calls
    out = run(w.research("agent web access", platforms=["hn", "news"], depth="quick", store=True))
    assert out.startswith("[WIRE | research:topic |") and "saved as run #1" in out
    assert w.store.search("agent")[0]["id"] == 1
    again = run(w.research("agent web access", platforms=["hn", "news"], depth="quick"))
    assert "## From your library" in again and "run #1" in again
    lib = w.library.build()
    feed = ET.parse(lib / "feed.xml").getroot()
    assert len(feed.findall("{http://www.w3.org/2005/Atom}entry")) == 1
    assert (lib / "runs" / "1.html").exists()
    assert "agent web access" in (lib / "index.html").read_text("utf-8")
    data = json.loads(run(w.research("agent web access", platforms=["hn"], emit="json")))
    assert data["counts"] == {"hackernews": 1}
    assert run(w.research("x", depth="nope")).startswith("[WIRE | error | research]")
    run(w.close())


def test_watch_reports_only_new_items(tmp_path):
    state = {"extra": False}
    stamp = int(NOW.timestamp())

    def hn(url, kw):
        hits = [
            {"objectID": "1", "title": "Wire agent web access", "points": 10, "created_at_i": stamp}
        ]
        if state["extra"]:
            hits.append({"objectID": "9", "title": "Wire agent web access v2", "points": 5,
                         "created_at_i": stamp})  # fmt: skip
        return fetched({"hits": hits}, url=url)

    w = make_wire(tmp_path, {"hn.algolia.com": hn})
    w.store.add_watch("Agent  Web Access", days=7, sources="hackernews")
    first = run(run_watches(w))
    assert "first run" in first and "Wire agent web access" in first
    state["extra"] = True
    second = run(run_watches(w))
    assert "1 new since run #1" in second and "v2" in second
    assert w.store.watches()[0]["last_run_id"] == 2
    with pytest.raises(ValueError):
        check_webhook("file:///etc/passwd")
    run(w.close())


def test_store_misc(tmp_path):
    s = Store(tmp_path / "r.db")
    s.add_watch("t")
    assert s.remove_watch("T") and not s.remove_watch("t")
    assert s.search("anything") == [] and s.new_items(99, None) == []
    s.close()


def test_mcp_research_tool(tmp_path):
    mcp = create_server(home=tmp_path)

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("wire_research", {"topic": "rust vs go", "preflight": True})

    text = run(go()).content[0].text
    assert "mode: compare" in text and "subjects: rust | go" in text
