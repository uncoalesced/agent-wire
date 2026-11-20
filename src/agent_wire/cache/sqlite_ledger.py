"""L2 on-disk cache: per-domain winning waterfall stage."""

import sqlite3
import time
from pathlib import Path

ROUTE_TTL_S = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_routes (
    domain TEXT PRIMARY KEY,
    winning_stage INTEGER NOT NULL,
    cf_clearance TEXT,
    user_agent TEXT,
    updated_at INTEGER NOT NULL
);
"""


class Ledger:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def get_winning_stage(self, domain: str, ttl_s: int = ROUTE_TTL_S) -> int | None:
        row = self._db.execute(
            "SELECT winning_stage FROM domain_routes WHERE domain = ? AND updated_at > ?",
            (domain, int(time.time()) - ttl_s),
        ).fetchone()
        return row[0] if row else None

    def get_route(self, domain: str, ttl_s: int = ROUTE_TTL_S) -> dict | None:
        row = self._db.execute(
            "SELECT winning_stage, cf_clearance, user_agent FROM domain_routes"
            " WHERE domain = ? AND updated_at > ?",
            (domain, int(time.time()) - ttl_s),
        ).fetchone()
        if row is None:
            return None
        return {"stage": row[0], "cf_clearance": row[1], "user_agent": row[2]}

    def routes(self) -> list[tuple[str, int]]:
        return self._db.execute(
            "SELECT domain, winning_stage FROM domain_routes ORDER BY updated_at DESC"
        ).fetchall()

    def clear_route(self, domain: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM domain_routes WHERE domain = ?", (domain,))

    def set_winning_stage(
        self,
        domain: str,
        stage: int,
        cf_clearance: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO domain_routes VALUES (?, ?, ?, ?, ?)",
                (domain, stage, cf_clearance, user_agent, int(time.time())),
            )

