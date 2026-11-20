"""Regression tests for audited scoring / brief bugs (offline)."""

from datetime import UTC, datetime, timedelta

from test_research import hit

from agent_wire.research import brief as briefmod
from agent_wire.research.cluster import cluster
from agent_wire.research.engine import ResearchResult
from agent_wire.research.planner import make_plan
from agent_wire.research.scoring import Item, score_items

NOW = datetime.now(UTC)


def item(src, url, **kw):
    return Item(hit("rust async", url, source=src, score=5, **kw), query="rust async", origin=src)


def test_undated_web_and_news_items_are_dropped_from_a_window():
    items = [item("web", "a"), item("news", "b"), item("hackernews", "c"),
             item("web", "d", when=NOW - timedelta(days=1))]  # fmt: skip
    out = score_items(items, "rust async", NOW - timedelta(days=7), NOW, 7)
    assert sorted(i.hit.url for i in out) == ["c", "d"]


def test_bare_as_of_date_prints_its_own_last_day():
    plan = make_plan("x", days=7, as_of="2026-11-20")
    assert "window: 2026-11-14 -> 2026-11-20 (7 days)" in plan.describe()
    r = ResearchResult(plan=plan)
    assert "Window 2026-11-14 → 2026-11-20" in briefmod.to_markdown(r)


def test_brief_footnotes_undated_items_that_were_kept():
    plan = make_plan("rust async", sources=["hn"], days=7)
    kept = score_items([item("hackernews", "c")], "rust async", plan.since, plan.until, 7)
    r = ResearchResult(plan=plan, clusters=cluster(kept))
    assert "1 items have no date" in briefmod.to_markdown(r)
