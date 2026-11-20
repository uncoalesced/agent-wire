"""Regression tests for audited research-store bugs (offline)."""

from types import SimpleNamespace

from test_research import NOW, hit

from agent_wire.research.planner import make_plan
from agent_wire.research.scoring import Item
from agent_wire.research.store import Store


def fake_result(topic="t", items=()):
    return SimpleNamespace(plan=make_plan(topic), created_at=NOW, items=list(items))


def test_new_items_lists_each_url_once(tmp_path):
    s = Store(tmp_path / "r.db")
    dup = [
        Item(hit("a", "https://e.com/x/"), score=0.9),
        Item(hit("a", "https://www.e.com/x"), score=0.5),
    ]
    rid = s.save(fake_result(items=dup), "md", {"headline": "h"})
    assert len(s.new_items(rid, None)) == 1
    s.close()
