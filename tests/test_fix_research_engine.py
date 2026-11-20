"""Regression tests for audited research-engine bugs (offline)."""

import asyncio

import pytest
from conftest import fetched
from test_research import make_wire, routes

from agent_wire.research.engine import Researcher
from agent_wire.research.watch import run_watches


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
