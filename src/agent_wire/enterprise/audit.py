"""WIRE_HOME/audit.jsonl: one signed, hash-chained line per tool call.

Each line carries `prev`, the SHA-256 of the previous line, and `sig`, an Ed25519 signature over the
rest of the line. Editing, deleting or reordering a line breaks the chain; truncating the tail is
caught by comparing the last sequence number against an earlier export. A line holds the tool, the
caller, the decision, the hosts reached (never paths or query strings) and a hash of the arguments:
no page text, no secrets, no raw arguments.

shortcut: one writer process per WIRE_HOME (a thread lock, no file lock); add a lock file if two
servers ever share one home.
"""

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agent_wire.auth.cookie_vault import _protect, _unprotect

GENESIS = "0" * 64


def _canon(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def _public_hex(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw.hex()


class AuditLog:
    def __init__(self, home: Path):
        self.path = home / "audit.jsonl"
        self.key_path = home / "audit.key"
        self.pub_path = home / "audit.pub"
        self._lock = threading.Lock()
        self._key = self._load_key(home)
        self._seq, self._prev = self._tail()

    def _load_key(self, home: Path) -> Ed25519PrivateKey:
        home.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            seed = _unprotect(self.key_path.read_bytes())
            return Ed25519PrivateKey.from_private_bytes(seed)
        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())  # fmt: skip
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_protect(seed))
        self.pub_path.write_text(_public_hex(key) + "\n", encoding="ascii")
        return key

    @property
    def public_key(self) -> str:
        return _public_hex(self._key)

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = ""
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line.rstrip("\n")
        if not last:
            return 0, GENESIS
        return json.loads(last)["seq"], _digest(last)

    def append(self, **fields) -> dict:
        with self._lock:
            record = {
                "seq": self._seq + 1,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "prev": self._prev,
                **fields,
            }
            record["sig"] = self._key.sign(_canon(record)).hex()
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq, self._prev = record["seq"], _digest(line)
            return record


def verify(path: Path, public_key_hex: str) -> tuple[int, str | None]:
    """(lines checked, None) for an intact log, or (lines checked before the fault, what broke)."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
    except ValueError:
        return 0, "the public key is not 32 bytes of hex"
    prev, seq = GENESIS, 0
    with path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line:
                continue
            try:
                rec = json.loads(line)
                sig = bytes.fromhex(rec.pop("sig"))
            except (ValueError, KeyError, TypeError):
                return seq, f"line {seq + 1} is not a signed record"
            if rec.get("seq") != seq + 1:
                return seq, f"line {seq + 1}: sequence {rec.get('seq')} breaks the order"
            if rec.get("prev") != prev:
                return seq, f"line {seq + 1}: chain broken (a line was changed, removed or moved)"
            try:
                pub.verify(sig, _canon(rec))
            except InvalidSignature:
                return seq, f"line {seq + 1}: signature does not match"
            prev, seq = _digest(line), seq + 1
    return seq, None
