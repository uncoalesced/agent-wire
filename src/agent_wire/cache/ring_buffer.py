"""L1 session-delta cache: zstd-compressed LRU capped at 64 entries and 16 MiB."""

import hashlib
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import zstandard

MAX_ENTRIES = 64
MAX_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class Entry:
    timestamp: float
    sha256: str
    comment_ids: frozenset[str]
    payload: bytes


@dataclass(slots=True)
class Delta:
    status: Literal["new", "unchanged", "delta", "changed"]
    age_s: float = 0.0
    new_comment_ids: frozenset[str] = frozenset()


class RingBuffer:
    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self._zc = zstandard.ZstdCompressor(level=3)
        self._zd = zstandard.ZstdDecompressor()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def get(self, key: str) -> Entry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def load(self, key: str) -> str | None:
        entry = self.get(key)
        return self._zd.decompress(entry.payload).decode() if entry else None

    def put(self, key: str, text: str, comment_ids: Iterable[str] = ()) -> Entry | None:
        data = text.encode()
        payload = self._zc.compress(data)
        self._remove(key)
        if len(payload) > self.max_bytes:
            return None  # bigger than the whole buffer: don't cache it
        self.evict_if_needed(len(payload))
        entry = Entry(
            time.time(), hashlib.sha256(data).hexdigest(), frozenset(comment_ids), payload
        )
        self._entries[key] = entry
        self.total_bytes += len(payload)
        return entry

    def evict_if_needed(self, new_bytes: int) -> None:
        while self._entries and (
            self.total_bytes + new_bytes > self.max_bytes or len(self._entries) >= self.max_entries
        ):
            _, old = self._entries.popitem(last=False)
            self.total_bytes -= len(old.payload)

    def observe(self, key: str, text: str, comment_ids: Iterable[str] = ()) -> Delta:
        """Compare against the last read of `key`, then store `text` unless unchanged."""
        ids = frozenset(comment_ids)
        prev = self.get(key)
        if prev is None:
            self.put(key, text, ids)
            return Delta("new")
        age = time.time() - prev.timestamp
        if prev.sha256 == hashlib.sha256(text.encode()).hexdigest():
            return Delta("unchanged", age)
        self.put(key, text, ids)
        new_ids = ids - prev.comment_ids
        return Delta("delta", age, new_ids) if new_ids else Delta("changed", age)

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self.total_bytes -= len(entry.payload)
