"""Research persistence (WIRE_HOME/research.db): saved runs, their items, and watched topics."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from agent_wire.channels.tier1_global.search import url_key
from agent_wire.compression.bm25_slicer import bm25_scores

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    mode TEXT NOT NULL,
    days INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    headline TEXT NOT NULL DEFAULT '',
    markdown TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url_key TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    ts TEXT,
    score REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_run ON items(run_id);
CREATE INDEX IF NOT EXISTS runs_topic ON runs(topic);
CREATE TABLE IF NOT EXISTS watches (
    topic TEXT PRIMARY KEY,
    days INTEGER NOT NULL DEFAULT 7,
    sources TEXT,
    webhook TEXT,
    created_at TEXT NOT NULL,
    last_run_id INTEGER
);
"""


def norm_topic(topic: str) -> str:
    return " ".join(topic.lower().split())


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- runs --------------------------------------------------------------------------------
    def save(self, result, markdown: str, data: dict) -> int:
        p = result.plan
        topic = norm_topic(p.topic) or ("discover" if p.mode == "discover" else p.github_user or "")
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (topic, mode, days, created_at, headline, markdown, json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (topic, p.mode, p.days, result.created_at.isoformat(), data.get("headline", ""),
                 markdown, json.dumps(data, ensure_ascii=False)),
            )  # fmt: skip
            run_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO items (run_id, url_key, url, title, source, ts, score)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, url_key(i.hit.url), i.hit.url, i.hit.title, i.source,
                  i.hit.timestamp.isoformat() if i.hit.timestamp else None, i.score)
                 for i in result.items],
            )  # fmt: skip
        return run_id

    def runs(self, limit: int = 20, topic: str | None = None) -> list[dict]:
        q, args = "SELECT id, topic, mode, days, created_at, headline FROM runs", []
        if topic:
            q, args = q + " WHERE topic = ?", [norm_topic(topic)]
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [dict(r) for r in rows]

    def run(self, run_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def items(self, run_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM items WHERE run_id = ? ORDER BY score DESC", (run_id,)
        )
        return [dict(r) for r in rows]

    def previous(self, topic: str, before_id: int) -> int | None:
        row = self.db.execute(
            "SELECT id FROM runs WHERE topic = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (norm_topic(topic), before_id),
        ).fetchone()
        return row["id"] if row else None

    def new_items(self, run_id: int, previous_id: int | None) -> list[dict]:
        """Items in this run whose URL never appeared in the previous run of the same topic."""
        if previous_id is None:
            return self.items(run_id)
        rows = self.db.execute("SELECT url_key FROM items WHERE run_id = ?", (previous_id,))
        seen = {r["url_key"] for r in rows}
        return [i for i in self.items(run_id) if i["url_key"] not in seen]

    def search(self, query: str, limit: int = 10, exclude: int | None = None) -> list[dict]:
        """Offline, deterministic BM25 over every saved brief."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT id, topic, mode, days, created_at, headline, markdown FROM runs"
            " ORDER BY id DESC LIMIT 2000")]  # fmt: skip
        rows = [r for r in rows if r["id"] != exclude]
        if not rows:
            return []
        texts = [f"{r['topic']} {r['topic']} {r['headline']} {r['markdown']}" for r in rows]
        scores = bm25_scores(texts, query)
        ranked = sorted(zip(scores, rows, strict=True), key=lambda sr: (-sr[0], -sr[1]["id"]))
        return [{k: v for k, v in r.items() if k != "markdown"} | {"score": round(s, 3)}
                for s, r in ranked[:limit] if s > 0]  # fmt: skip

    # ---- watches -----------------------------------------------------------------------------
    def add_watch(self, topic: str, days: int = 7, sources: str | None = None,
                  webhook: str | None = None) -> None:  # fmt: skip
        with self.db:
            self.db.execute(
                "INSERT INTO watches (topic, days, sources, webhook, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(topic) DO UPDATE SET days = excluded.days,"
                " sources = excluded.sources, webhook = excluded.webhook",
                (norm_topic(topic), days, sources, webhook, datetime.now(UTC).isoformat()),
            )

    def remove_watch(self, topic: str) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM watches WHERE topic = ?", (norm_topic(topic),))
        return cur.rowcount > 0

    def watches(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM watches ORDER BY topic")]

    def mark_watched(self, topic: str, run_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE watches SET last_run_id = ? WHERE topic = ?",
                            (run_id, norm_topic(topic)))  # fmt: skip
