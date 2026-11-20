"""Regression tests for audited research-store bugs (offline)."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

from test_research import NOW, hit

from agent_wire.research.planner import make_plan
from agent_wire.research.scoring import Item
from agent_wire.research.store import Store


def fake_result(topic="t", items=(), created_at=NOW):
    return SimpleNamespace(plan=make_plan(topic), created_at=created_at, items=list(items))


def test_new_items_lists_each_url_once(tmp_path):
    s = Store(tmp_path / "r.db")
    dup = [
        Item(hit("a", "https://e.com/x/"), score=0.9),
        Item(hit("a", "https://www.e.com/x"), score=0.5),
    ]
    rid = s.save(fake_result(items=dup), "md", {"headline": "h"})
    assert len(s.new_items(rid, None)) == 1
    s.close()


def test_re_adding_a_watch_keeps_its_sources_and_webhook(tmp_path):
    s = Store(tmp_path / "r.db")
    s.add_watch("T", days=7, sources="hn,github", webhook="https://h.example/x")
    s.add_watch("t", days=3)
    w = s.watches()[0]
    assert (w["days"], w["sources"], w["webhook"]) == (3, "hn,github", "https://h.example/x")
    s.add_watch("t", sources="reddit")
    assert s.watches()[0]["sources"] == "reddit" and s.watches()[0]["webhook"]
    s.close()


def test_saved_json_has_its_run_id_and_no_secrets(tmp_path):
    s = Store(tmp_path / "r.db")
    key = "sk-ant-api03-abcdefghij1234567890ABCDEF"
    data = {"headline": f"leaked {key}", "run_id": None}
    rid = s.save(fake_result(), f"# t\nkey {key}", data)
    row = s.run(rid)
    assert json.loads(row["json"])["run_id"] == rid == data["run_id"]
    assert key not in row["markdown"] and key not in row["json"] and key not in row["headline"]
    s.close()


def test_retention_keeps_newest_runs_and_drops_old_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("WIRE_RESEARCH_KEEP", "3")
    s = Store(tmp_path / "r.db")
    for n in range(5):
        s.save(fake_result(f"topic {n}", [Item(hit("a", f"https://e.com/{n}"))]), "md", {})
    assert [r["id"] for r in s.runs()] == [5, 4, 3]
    assert s.items(1) == [] and len(s.items(5)) == 1  # items go with their run
    monkeypatch.delenv("WIRE_RESEARCH_KEEP")
    s.save(fake_result("ancient", created_at=NOW - timedelta(days=200)), "md", {})
    s.save(fake_result("fresh"), "md", {})  # a save prunes runs past 180 days
    assert "ancient" not in {r["topic"] for r in s.runs()}
    s.close()


def test_search_uses_summaries_backfills_old_dbs_and_runs_in_a_thread(tmp_path):
    s = Store(tmp_path / "r.db")
    md = "# x\n\n1. **[Quantum widgets ship](https://e.com/1)** · hackernews\n" + "filler " * 5000
    s.save(fake_result("chips"), md, {"headline": "h"})
    s.db.execute("UPDATE runs SET summary = NULL")  # a database from before the column
    s.db.commit()
    assert [h["id"] for h in s.search("quantum")] == [1]
    assert s.db.execute("SELECT length(summary) FROM runs").fetchone()[0] < 200  # backfilled
    assert "markdown" not in s.search("quantum")[0] and "summary" not in s.search("quantum")[0]
    assert asyncio.run(s.search_async("quantum")) == s.search("quantum")
    s.close()


def test_old_database_without_summary_column_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,"
               " mode TEXT NOT NULL, days INTEGER NOT NULL, created_at TEXT NOT NULL,"
               " headline TEXT NOT NULL DEFAULT '', markdown TEXT NOT NULL, json TEXT NOT NULL)")  # fmt: skip
    db.execute(
        "INSERT INTO runs VALUES (1,'rust','topic',7,'2026-01-01','h','# a\\n1. **[Rust 2](u)**','{}')"
    )
    db.commit()
    db.close()
    s = Store(path)
    assert [h["id"] for h in s.search("rust")] == [1]
    s.close()
