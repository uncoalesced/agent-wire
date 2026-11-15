# Engineered by uncoalesced
"""WIRE_HOME/alerts.db: the local alert ledger, chained by hash so edits are detectable.

Tamper-evident, not tamper-proof: someone who can rewrite WIRE_HOME can rewrite the chain too,
and that is out of scope (SECURITY.md). It catches accidental edits and naive tampering.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from agent_wire.shield.events import Alert

FIELDS = ("ts", "session_id", "tool", "channel", "url", "layer", "rule_id", "tier", "technique",
          "location", "snippet", "decoded", "action", "bytes_removed")  # fmt: skip

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, session_id TEXT, tool TEXT, channel TEXT, url TEXT, layer TEXT,
    rule_id TEXT, tier INTEGER, technique TEXT, location TEXT, snippet TEXT, decoded TEXT,
    action TEXT, bytes_removed INTEGER, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (rule_id TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""
GENESIS = "0" * 64
_INSERT = (  # column names are the FIELDS constant above; values are bound parameters
    f"INSERT INTO alerts ({', '.join(FIELDS)}, prev_hash, hash)"  # nosec B608
    f" VALUES ({', '.join('?' * len(FIELDS))}, ?, ?)"
)


def _digest(prev: str, row: dict) -> str:
    body = json.dumps([prev, *(row[f] for f in FIELDS)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class AlertLedger:
    def __init__(self, path: Path):
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def add(self, alert: Alert) -> int:
        row = {f: getattr(alert, f) for f in FIELDS}
        with self._db:
            last = self._db.execute("SELECT hash FROM alerts ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            cur = self._db.execute(_INSERT, (*row.values(), prev, _digest(prev, row)))
        return cur.lastrowid

    def bump(self, counts: dict[str, int]) -> None:
        if not counts:
            return
        with self._db:
            self._db.executemany(
                "INSERT INTO counters (rule_id, n) VALUES (?, ?)"
                " ON CONFLICT(rule_id) DO UPDATE SET n = n + excluded.n",
                list(counts.items()),
            )

    def recent(self, limit: int = 20, min_tier: int = 1) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM alerts WHERE tier >= ? ORDER BY id DESC LIMIT ?", (min_tier, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, alert_id: int) -> dict | None:
        r = self._db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return dict(r) if r else None

    def after(self, alert_id: int) -> list[dict]:
        rows = self._db.execute("SELECT * FROM alerts WHERE id > ? ORDER BY id", (alert_id,))
        return [dict(r) for r in rows.fetchall()]

    def last_id(self) -> int:
        r = self._db.execute("SELECT MAX(id) AS m FROM alerts").fetchone()
        return r["m"] or 0

    def summary(self) -> dict:
        tiers = dict(self._db.execute("SELECT tier, COUNT(*) FROM alerts GROUP BY tier").fetchall())
        counters = dict(self._db.execute("SELECT rule_id, n FROM counters").fetchall())
        return {"tiers": tiers, "counters": counters}

    def verify(self) -> tuple[bool, int | None]:
        """(chain intact, first bad id)."""
        prev = GENESIS
        for r in self._db.execute("SELECT * FROM alerts ORDER BY id"):
            row = {f: r[f] for f in FIELDS}
            if r["prev_hash"] != prev or r["hash"] != _digest(prev, row):
                return False, r["id"]
            prev = r["hash"]
        return True, None
