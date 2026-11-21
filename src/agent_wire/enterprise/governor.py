"""The gate every tool call passes: policy, budgets, loop stop, kill switch and audit trail.

`admit` runs before a tool does anything and raises `PolicyDenied` with a stable code an agent can
act on (`POLICY_DENIED`, `BUDGET_EXCEEDED`, `LOOP_DETECTED`, `KILLED`, `FORBIDDEN`). `url_gate` is
installed into the SSRF guard, so it sees every URL the server is about to fetch, redirects included.
`done` writes the audit line.

shortcut: budgets live in memory and reset when the server restarts; persist them if a restart must
not hand an agent a fresh day.
"""

import hashlib
import time
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from agent_wire.engine.errors import WireBlockedError
from agent_wire.enterprise.audit import AuditLog
from agent_wire.enterprise.jwt_auth import Principal
from agent_wire.enterprise.policy import Policy

LOOP_WINDOW_S = 60
_hosts: ContextVar[list[str] | None] = ContextVar("wire_call_hosts", default=None)


class PolicyDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def render(self, tool: str) -> str:
        return f"[WIRE | {self.code} | {tool}] {self.message}"


class Governor:
    def __init__(self, home: Path, policy: Policy, audit: AuditLog | None = None,
                 clock=time.time):  # fmt: skip
        self.home, self.policy, self.audit, self.clock = home, policy, audit, clock
        self._calls: deque[float] = deque()
        self._recent: deque[tuple[float, str]] = deque()
        self._day = self._today()
        self._day_calls = self._day_bytes = 0

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        if (today := self._today()) != self._day:
            self._day, self._day_calls, self._day_bytes = today, 0, 0

    def remaining(self) -> dict[str, int | None]:
        self._roll()
        p, now = self.policy, self.clock()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()

        def left(limit: int, used: int) -> int | None:
            return max(limit - used, 0) if limit else None

        return {
            "calls_this_minute": left(p.max_calls_per_minute, len(self._calls)),
            "calls_today": left(p.max_calls_per_day, self._day_calls),
            "bytes_today": left(p.max_bytes_per_day, self._day_bytes),
        }

    @staticmethod
    def fingerprint(args_repr: str) -> str:
        return hashlib.sha256(args_repr.encode()).hexdigest()[:16]

    def admit(self, tool: str, caller: Principal | None, call: str) -> None:
        """Allow the call (`call` is its argument fingerprint) or raise PolicyDenied."""
        _hosts.set([])
        p, now = self.policy, self.clock()
        if (self.home / p.kill_switch).exists():
            raise PolicyDenied("KILLED", "an operator stopped this server; do not retry")
        if tool in p.disabled_tools:
            raise PolicyDenied("POLICY_DENIED", f"{tool} is disabled by policy; do not retry")
        if caller is not None and not caller.allows(tool):
            raise PolicyDenied("FORBIDDEN", f"this token is not allowed to call {tool}")
        left = self.remaining()
        if left["calls_this_minute"] == 0:
            raise PolicyDenied("BUDGET_EXCEEDED", "per-minute call budget spent; wait 60 seconds")
        if left["calls_today"] == 0 or left["bytes_today"] == 0:
            raise PolicyDenied("BUDGET_EXCEEDED", "daily budget spent; stop until tomorrow (UTC)")
        if p.max_repeat_calls:
            while self._recent and now - self._recent[0][0] > LOOP_WINDOW_S:
                self._recent.popleft()
            if sum(1 for _, c in self._recent if c == call) >= p.max_repeat_calls:
                raise PolicyDenied(
                    "LOOP_DETECTED", "the same call keeps repeating; change it or stop"
                )
        self._recent.append((now, call))
        self._calls.append(now)
        self._day_calls += 1

    def url_gate(self, url: str) -> None:
        if (why := self.policy.url_refusal(url)) is not None:
            raise WireBlockedError(f"{url.split('?')[0]}: blocked by policy: {why}")
        if (hosts := _hosts.get()) is not None:
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            if host and host not in hosts and len(hosts) < 50:
                hosts.append(host)

    def status_line(self) -> str:
        left = ", ".join(
            f"{k}={'unlimited' if v is None else v}" for k, v in self.remaining().items()
        )
        return f"governed: {left}" + f" · kill switch: {self.home / self.policy.kill_switch}"

    def done(self, tool: str, caller: Principal | None, call: str, decision: str, ms: int,
             out_bytes: int = 0, code: str = "") -> None:  # fmt: skip
        self._day_bytes += out_bytes
        if self.audit is not None:
            self.audit.append(tool=tool, caller=caller.subject if caller else "local",
                              decision=decision, code=code, call=call, ms=ms, bytes=out_bytes,
                              hosts=list(_hosts.get() or []))  # fmt: skip


def build(home: Path, *, autonomous: bool = False) -> Governor | None:
    """A governor when the operator wrote a policy (or asked for --autonomous); None otherwise."""
    from agent_wire.enterprise.policy import load

    policy = load(home, autonomous=autonomous)
    if policy is None:
        return None
    return Governor(home, policy, AuditLog(home) if policy.audit else None)
