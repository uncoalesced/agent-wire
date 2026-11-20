"""Research persistence (WIRE_HOME/research.db): saved runs, their items, and watched topics."""

import asyncio
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_wire.channels.tier1_global.search import url_key
from agent_wire.compression.bm25_slicer import bm25_scores
from agent_wire.firewall.outbound_privacy import scrub

KEEP_RUNS = 500  # newest runs kept; override with WIRE_RESEARCH_KEEP
KEEP_DAYS = 180
SUMMARY_CHARS = 1500
_STORY = re.compile(r"^\d+\. \*\*\[(.+?)\]\(", re.MULTILINE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    mode TEXT NOT NULL,
    days INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    headline TEXT NOT NULL DEFAULT '',
    markdown TEXT NOT NULL,
    json TEXT NOT NULL,
    summary TEXT
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


def summarize(markdown: str) -> str:
    """A brief's story titles: search text of a few hundred bytes instead of the whole page."""
    titles = _STORY.findall(markdown)
    return (" · ".join(titles) or " ".join(markdown.split()))[:SUMMARY_CHARS]


def _scrubbed(obj):
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: _scrubbed(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrubbed(v) for v in obj]
    return obj


def _keep() -> int:
    try:
        return max(1, int(os.environ.get("WIRE_RESEARCH_KEEP", KEEP_RUNS)))
    except ValueError:
        return KEEP_RUNS


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        if "summary" not in {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN summary TEXT")  # older db; filled lazily

    def close(self) -> None:
        self.db.close()

    # ---- runs --------------------------------------------------------------------------------
    def save(self, result, markdown: str, data: dict) -> int:
        p = result.plan
        topic = norm_topic(p.topic) or ("discover" if p.mode == "discover" else p.github_user or "")
        markdown, clean = scrub(markdown), _scrubbed(data)
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (topic, mode, days, created_at, headline, markdown, json, summary)"
                " VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
                (topic, p.mode, p.days, result.created_at.isoformat(), clean.get("headline", ""),
                 markdown, summarize(markdown)),
            )  # fmt: skip
            run_id = cur.lastrowid
            data["run_id"] = clean["run_id"] = run_id  # the stored JSON knows its own id
            self.db.execute("UPDATE runs SET json = ? WHERE id = ?",
                            (json.dumps(clean, ensure_ascii=False), run_id))  # fmt: skip
            self.db.executemany(
                "INSERT INTO items (run_id, url_key, url, title, source, ts, score)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(run_id, url_key(i.hit.url), i.hit.url, scrub(i.hit.title), i.source,
                  i.hit.timestamp.isoformat() if i.hit.timestamp else None, i.score)
                 for i in result.items],
            )  # fmt: skip
            self._prune(run_id)
        return run_id

    def _prune(self, keep_id: int) -> None:
        """Retention: the newest WIRE_RESEARCH_KEEP runs, none older than KEEP_DAYS (items cascade)."""
        cutoff = (datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat()
        self.db.execute(
            "DELETE FROM runs WHERE id != ? AND (created_at < ?"
            " OR id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?))",
            (keep_id, cutoff, _keep()),
        )

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
        rows = self.db.execute("SELECT url_key FROM items WHERE run_id = ?", (previous_id,))
        seen = {r["url_key"] for r in rows}  # empty when there is no previous run
        out = []
        for i in self.items(run_id):  # best-scored copy first; a URL is listed once
            if i["url_key"] not in seen:
                seen.add(i["url_key"])
                out.append(i)
        return out

    def search(self, query: str, limit: int = 10, exclude: int | None = None) -> list[dict]:
        """Offline, deterministic BM25 over saved briefs' topic, headline and story titles."""
        return self._search(self.db, query, limit, exclude)

    async def search_async(self, query: str, limit: int = 10, exclude: int | None = None):
        """`search` off the event loop, on its own connection (sqlite objects stay in a thread)."""
        return await asyncio.to_thread(self._search_own_conn, query, limit, exclude)

    def _search_own_conn(self, query: str, limit: int, exclude: int | None) -> list[dict]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            return self._search(db, query, limit, exclude)
        finally:
            db.close()

    @staticmethod
    def _search(db, query: str, limit: int, exclude: int | None) -> list[dict]:
        rows = [dict(r) for r in db.execute(
            "SELECT id, topic, mode, days, created_at, headline, summary FROM runs"
            " ORDER BY id DESC LIMIT 2000")]  # fmt: skip
        if todo := [r for r in rows if r["summary"] is None]:  # saved before the column existed
            with db:
                for r in todo:
                    md = db.execute("SELECT markdown FROM runs WHERE id = ?", (r["id"],)).fetchone()
                    r["summary"] = summarize(md["markdown"])
                    db.execute("UPDATE runs SET summary = ? WHERE id = ?", (r["summary"], r["id"]))
        rows = [r for r in rows if r["id"] != exclude]
        if not rows:
            return []
        texts = [f"{r['topic']} {r['topic']} {r['headline']} {r['summary']}" for r in rows]
        scores = bm25_scores(texts, query)
        ranked = sorted(zip(scores, rows, strict=True), key=lambda sr: (-sr[0], -sr[1]["id"]))
        return [{k: v for k, v in r.items() if k != "summary"} | {"score": round(s, 3)}
                for s, r in ranked[:limit] if s > 0]  # fmt: skip

    # ---- watches -----------------------------------------------------------------------------
    def add_watch(self, topic: str, days: int = 7, sources: str | None = None,
                  webhook: str | None = None) -> None:  # fmt: skip
        """Re-adding a topic without --sources / --webhook keeps the ones it already had."""
        with self.db:
            self.db.execute(
                "INSERT INTO watches (topic, days, sources, webhook, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(topic) DO UPDATE SET days = excluded.days,"
                " sources = COALESCE(excluded.sources, sources),"
                " webhook = COALESCE(excluded.webhook, webhook)",
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
