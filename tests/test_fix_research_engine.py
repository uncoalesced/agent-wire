"""Regression tests for audited research-engine bugs (offline)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import fetched
from test_research import make_wire, routes

from agent_wire.research.engine import Researcher
from agent_wire.research.watch import run_watches

NOW = datetime.now(UTC)


def run(coro):
    return asyncio.run(coro)


def test_odd_payload_in_one_source_is_a_gap_not_an_abort(tmp_path):
    rts = routes()
    rts["lobste.rs/hottest.json"] = fetched({"error": "rate limited"})
    w = make_wire(tmp_path, rts)
    r = run(Researcher(w).run("agent web access", sources=["hn", "lobsters"], depth="quick"))
    assert r.counts == {"hackernews": 1}
    assert "lobsters" in r.gaps and "TypeError" in r.gaps["lobsters"]
    run(w.close())


def test_discover_survives_a_dict_where_a_list_belongs(tmp_path):
    rts = routes()
    rts["lobste.rs/hottest.json"] = fetched({"error": "x"})
    rts["mastodon.social"] = fetched({"error": "x"})
    w = make_wire(tmp_path, rts)
    r = run(Researcher(w).run("", sources=["hn", "lobsters", "mastodon"], depth="quick"))
    assert {"lobsters", "mastodon"} <= set(r.gaps)
    run(w.close())


def test_one_bad_watch_does_not_stop_the_others(tmp_path):
    w = make_wire(tmp_path, routes())
    w.store.add_watch("agent web access", days=7, sources="hackernews")
    w.store.add_watch("zzz stale", days=7, sources="myspace")  # stale source name
    out = run(run_watches(w))
    assert "zzz stale · FAILED: ValueError" in out and "agent web access · run #" in out
    run(w.close())


def test_cancellation_is_not_swallowed(tmp_path):
    w = make_wire(tmp_path, routes())

    async def boom(*a, **k):
        raise asyncio.CancelledError

    r = Researcher(w)
    r._search = boom
    with pytest.raises(asyncio.CancelledError):
        run(r.run("agent web access", sources=["hn"], depth="quick"))
    run(w.close())


def test_same_post_from_two_queries_is_counted_once(tmp_path):
    stamp = int((NOW - timedelta(hours=5)).timestamp())
    hn = {"hits": [{"objectID": "1", "title": "Wire agent web access", "points": 80,
                    "num_comments": 30, "created_at_i": stamp}]}  # fmt: skip
    w = make_wire(tmp_path, {"hn.algolia.com/api/v1/search": hn})
    topic = "what is the agent web access"  # deep depth runs the topic and its core terms
    r = run(Researcher(w).run(topic, sources=["hn"], depth="deep"))
    assert r.counts == {"hackernews": 1} and len(r.items) == 1
    assert r.clusters[0].engagement == {"score": 80, "comments": 30}
    assert r.items[0].queries == [topic, "agent web access"]
    run(w.close())
