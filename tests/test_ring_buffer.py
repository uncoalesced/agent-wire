import os
import sqlite3

from agent_wire.cache.ring_buffer import MAX_BYTES, MAX_ENTRIES, RingBuffer
from agent_wire.cache.sqlite_ledger import Ledger


def test_100mb_of_payloads_stays_under_16mb():
    rb = RingBuffer()
    for i in range(100):
        rb.put(f"https://example.com/{i}", os.urandom(512 * 1024).hex())  # 1 MB of text each
        assert rb.total_bytes <= MAX_BYTES
    assert rb.total_bytes == sum(len(e.payload) for e in rb._entries.values())
    assert 0 < len(rb) < 100
    assert "https://example.com/99" in rb


def test_entry_cap():
    rb = RingBuffer()
    for i in range(200):
        rb.put(str(i), "x")
    assert len(rb) == MAX_ENTRIES
    assert "199" in rb and "0" not in rb


def test_lru_evicts_least_recently_used():
    rb = RingBuffer(max_entries=2)
    rb.put("a", "1")
    rb.put("b", "2")
    rb.get("a")
    rb.put("c", "3")
    assert "a" in rb and "c" in rb and "b" not in rb


def test_oversized_payload_not_cached():
    rb = RingBuffer(max_bytes=100)
    assert rb.put("big", os.urandom(10_000).hex()) is None
    assert len(rb) == 0 and rb.total_bytes == 0


def test_replace_same_key_keeps_byte_count_exact():
    rb = RingBuffer()
    rb.put("k", "a" * 1000)
    rb.put("k", os.urandom(1000).hex())
    assert len(rb) == 1
    assert rb.total_bytes == len(rb.get("k").payload)


def test_observe_new_unchanged_delta_changed():
    rb = RingBuffer()
    assert rb.observe("t", "thread v1", ["c1", "c2"]).status == "new"
    assert rb.observe("t", "thread v1", ["c1", "c2"]).status == "unchanged"

    d = rb.observe("t", "thread v2", ["c1", "c2", "c3", "c4"])
    assert d.status == "delta"
    assert d.new_comment_ids == {"c3", "c4"}
    assert d.age_s >= 0

    assert rb.observe("t", "thread v3 edited", ["c1"]).status == "changed"
    assert rb.load("t") == "thread v3 edited"


def test_ledger_caps_at_500_rows_keeping_newest(tmp_path):
    ledger = Ledger(tmp_path / "cache.db")
    for i in range(600):
        ledger.put_cached(f"https://example.com/{i}", content_hash=str(i), body=b"x" * 100)
    assert ledger.cache_rows() == 500
    assert ledger.get_cached("https://example.com/0") is None
    assert ledger.get_cached("https://example.com/599")["body"] == b"x" * 100
    ledger.close()


def test_ledger_winning_stage_and_ttl(tmp_path):
    ledger = Ledger(tmp_path / "cache.db")
    assert ledger.get_winning_stage("reddit.com") is None
    ledger.set_winning_stage("reddit.com", 2)
    assert ledger.get_winning_stage("reddit.com") == 2
    assert ledger.get_winning_stage("reddit.com", ttl_s=-1) is None  # expired
    ledger.close()


def test_ledger_uses_wal(tmp_path):
    Ledger(tmp_path / "cache.db").close()
    with sqlite3.connect(tmp_path / "cache.db") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
