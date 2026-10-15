"""L2 on-disk cache: per-domain winning waterfall stage + 500-row circular HTTP cache."""

import hashlib
import sqlite3
import time
from pathlib import Path

import zstandard

ROUTE_TTL_S = 24 * 3600

# rowid breaks updated_at ties (many inserts within one second) so the oldest row goes first.
SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_routes (
    domain TEXT PRIMARY KEY,
    winning_stage INTEGER NOT NULL,
    cf_clearance TEXT,
    user_agent TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS http_etag_cache (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_hash TEXT NOT NULL,
    compressed_body BLOB,
    updated_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS limit_etag_cache_size
AFTER INSERT ON http_etag_cache
WHEN (SELECT COUNT(*) FROM http_etag_cache) > 500
BEGIN
    DELETE FROM http_etag_cache
    WHERE url_hash IN (
        SELECT url_hash FROM http_etag_cache
        ORDER BY updated_at ASC, rowid ASC
        LIMIT (SELECT COUNT(*) - 500 FROM http_etag_cache)
    );
END;
"""


class Ledger:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._zc = zstandard.ZstdCompressor(level=3)
        self._zd = zstandard.ZstdDecompressor()

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

    def get_cached(self, url: str) -> dict | None:
        row = self._db.execute(
            "SELECT etag, last_modified, content_hash, compressed_body"
            " FROM http_etag_cache WHERE url_hash = ?",
            (_url_hash(url),),
        ).fetchone()
        if row is None:
            return None
        etag, last_modified, content_hash, blob = row
        body = self._zd.decompress(blob) if blob is not None else None
        return {
            "etag": etag,
            "last_modified": last_modified,
            "content_hash": content_hash,
            "body": body,
        }

    def put_cached(
        self,
        url: str,
        content_hash: str,
        body: bytes | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        blob = self._zc.compress(body) if body is not None else None
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO http_etag_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_url_hash(url), url, etag, last_modified, content_hash, blob, int(time.time())),
            )

    def cache_rows(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM http_etag_cache").fetchone()[0]


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()
