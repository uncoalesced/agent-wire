"""`wire selftest`: check every subsystem in one run.

Offline (default) drives each part through its real entry point with canned network data, inside
a guard that fails the run if anything reaches the network. `--live` adds one real request per
channel and the shield false-positive benchmark.
"""

import asyncio
import contextlib
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Credentials and switches that would make offline results depend on this machine.
ISOLATE_ENV = ("GITHUB_TOKEN", "GH_TOKEN", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "EXA_API_KEY",
               "WIRE_SEARXNG_URL", "WIRE_TRANSCRIBE", "WIRE_TRANSCRIBE_API_KEY", "WIRE_TRANSCRIBE_URL",
               "WIRE_ALLOW_PRIVATE", "WIRE_SHIELD_VERBOSE")  # fmt: skip

STATUSES = ("ok", "degraded", "skip", "fail")


@dataclass
class Result:
    group: str
    name: str
    status: str  # ok | degraded | skip | fail
    detail: str
    ms: int = 0


class _Env:
    """Temporarily set/unset environment variables."""

    def __init__(self, set_: dict[str, str], unset: tuple[str, ...]):
        self.set_, self.unset, self.saved = set_, unset, {}

    def __enter__(self):
        for k in (*self.set_, *self.unset):
            self.saved[k] = os.environ.get(k)
        for k in self.unset:
            os.environ.pop(k, None)
        os.environ.update(self.set_)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _scratch(home: Path | None) -> Path:
    from agent_wire.config import init_wire_home

    base = (home or init_wire_home()) / "selftest" / f"run-{os.getpid()}-{int(time.time() * 1000)}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _run_one(check, home: Path) -> Result:
    from agent_wire.selftest.checks import Degraded

    t0 = time.perf_counter()
    home.mkdir(parents=True, exist_ok=True)
    try:
        # The canned attack pages raise shield alerts by design; keep their stderr lines quiet.
        # A real file, not StringIO: the stdio check hands stderr's fileno to a subprocess.
        with open(os.devnull, "w") as sink, contextlib.redirect_stderr(sink):
            detail, status = asyncio.run(check.fn(home)), "ok"
    except Degraded as e:
        detail, status = str(e), "degraded"
    except Exception as e:  # noqa: BLE001 - every failure becomes a row, the run goes on
        detail, status = f"{type(e).__name__}: {e}"[:400], "fail"
    return Result(check.group, check.name, status, detail, int((time.perf_counter() - t0) * 1000))


def run_offline(only: str | None = None, home: Path | None = None) -> list[Result]:
    from agent_wire.selftest.checks import offline_checks
    from agent_wire.selftest.fakes import no_network

    checks = [c for c in offline_checks() if only in (None, c.group, c.name)]
    if not checks:
        raise ValueError(f"no check or group named {only!r}")
    scratch = _scratch(home)
    attempts: list[str] = []
    results = []
    try:
        with _Env({"WIRE_SHIELD_NOTIFY": "0", "WIRE_NO_GH_CLI": "1"}, ISOLATE_ENV), \
                no_network(attempts):  # fmt: skip
            for check in checks:
                results.append(_run_one(check, scratch / f"{check.group}-{len(results)}"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    results.append(Result("isolation", "no network used",
                          "fail" if attempts else "ok",
                          "; ".join(sorted(set(attempts)))[:400] or "0 network attempts"))  # fmt: skip
    return results


def run_live(only: str | None = None, home: Path | None = None) -> list[Result]:
    from agent_wire.channels import MODULES
    from agent_wire.selftest import live
    from agent_wire.server import Wire

    names = [n for n in MODULES if only in (None, "channels", "live", n)]
    scratch = _scratch(home)
    results = []

    async def go():
        wire = Wire(home=scratch)  # cold, like a fresh install
        try:
            for name in names:
                t0 = time.perf_counter()
                try:
                    status, detail = await live.probe(wire, name)
                except Exception as e:  # noqa: BLE001
                    status, detail = "fail", f"{type(e).__name__}: {e}"[:300]
                results.append(Result("live", name, status, detail,
                                      int((time.perf_counter() - t0) * 1000)))  # fmt: skip
            if only in (None, "live", "shield"):
                t0 = time.perf_counter()
                read, flagged, notes = await live.false_positives(wire)
                detail = f"{flagged}/{read} benchmark pages raised an alert"
                if notes:
                    detail += " · " + " · ".join(notes)
                results.append(Result("live", "shield false positives",
                                      "ok" if flagged == 0 else "degraded", detail[:600],
                                      int((time.perf_counter() - t0) * 1000)))  # fmt: skip
        finally:
            await wire.close()

    # Keyless Reddit is part of what is being checked: hide any configured app credentials.
    try:
        with _Env({"WIRE_SHIELD_NOTIFY": "0"}, ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")):
            asyncio.run(go())
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return results


def run(live: bool = False, only: str | None = None, home: Path | None = None) -> list[Result]:
    results = [] if only == "live" else run_offline(only, home)
    if live:
        results += run_live(only, home)
    return results


def failed(results: list[Result]) -> bool:
    return any(r.status == "fail" for r in results)


def summary(results: list[Result]) -> str:
    n = {s: sum(r.status == s for r in results) for s in STATUSES}
    return (f"{n['ok']} passed, {n['fail']} failed, {n['degraded']} degraded, {n['skip']} skipped "
            f"({len(results)} checks)")  # fmt: skip


def render(results: list[Result]) -> str:
    from agent_wire import __version__

    mark = {"ok": "PASS", "fail": "FAIL", "degraded": "WARN", "skip": "SKIP"}
    width = max((len(f"{r.group}/{r.name}") for r in results), default=10)
    lines = [f"agent-wire {__version__} selftest"]
    for r in results:
        lines.append(f"{mark[r.status]}  {f'{r.group}/{r.name}':<{width}}  {r.detail}  ({r.ms} ms)")
    lines.append(summary(results))
    return "\n".join(lines)


def as_dicts(results: list[Result]) -> list[dict]:
    return [asdict(r) for r in results]
