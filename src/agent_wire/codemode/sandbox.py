"""Run one async JavaScript snippet in QuickJS, with `wire.*` calls served by the host.

The script gets no filesystem, network, timers or modules. A `wire.x(...)` call only queues a
request in an outbox that the host drains between JS steps, so QuickJS never calls back into
Python (that keeps its CPU limit enforceable) and `Promise.all` fans calls out in parallel.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import quickjs

# (method name, positional args from JS) -> (JSON-able result, raw tokens fetched to produce it)
HostCall = Callable[[str, list], Awaitable[tuple[object, int]]]


@dataclass(frozen=True, slots=True)
class Limits:
    wall_s: float = 60.0
    cpu_s: float = 5.0  # per JS step; time spent awaiting wire calls is not counted
    memory_bytes: int = 64 << 20
    max_calls: int = 40
    max_inflight: int = 16
    log_chars: int = 8000


DEFAULT_LIMITS = Limits()


@dataclass(slots=True)
class Outcome:
    text: str | None = None  # what the script returned: a string as-is, anything else as JSON
    error: str | None = None
    logs: str = ""
    calls: int = 0
    raw_tokens: int = 0


class _Abort(Exception):
    pass


_PRELUDE = r"""
(() => {
  const pending = new Map(), queue = [], logs = [];
  let next = 0, logSize = 0, out = null;
  class WireError extends Error { constructor(m) { super(m); this.name = "WireError"; } }
  const call = (name, args) => new Promise((res, rej) => {
    const id = ++next; pending.set(id, {res, rej}); queue.push([id, name, args]);
  });
  const show = x => { if (typeof x === "string") return x;
    try { return JSON.stringify(x); } catch (e) { return String(x); } };
  const log = (...a) => { if (logSize > LOG_CAP) return;
    const s = a.map(show).join(" "); logSize += s.length + 1; logs.push(s); };
  const line = e => { const m = /<input>:(\d+)/.exec((e && e.stack) || ""); return m ? +m[1] : null; };
  globalThis.console = Object.freeze({log, info: log, warn: log, error: log, debug: log});
  globalThis.wire = Object.freeze(Object.fromEntries(NAMES.map(n => [n, (...a) => call(n, a)])));
  globalThis.__finish = p => p.then(v => {
    if (typeof v === "string") { out = {text: v}; return; }
    try { out = {text: JSON.stringify(v === undefined ? null : v) ?? "null"}; }
    catch (e) { out = {error: "TypeError: return value is not JSON-serializable: " + e.message}; }
  }, e => {
    out = e instanceof Error
      ? {error: `${e.name}: ${e.message}`, line: e instanceof WireError ? null : line(e)}
      : {error: "Uncaught: " + show(e)};
  });
  globalThis.__drain = () => JSON.stringify(queue.splice(0));
  globalThis.__settle = (id, ok, payload) => {
    const p = pending.get(id); pending.delete(id);
    ok ? p.res(JSON.parse(payload)) : p.rej(new WireError(payload));
  };
  globalThis.__state = () => out === null ? "" : JSON.stringify(out);
  globalThis.__logs = () => logs.join("\n");
})();
"""


def _prelude(names: tuple[str, ...], log_chars: int) -> str:
    return _PRELUDE.replace("NAMES", json.dumps(list(names))).replace("LOG_CAP", str(log_chars))


def _js_error(exc: quickjs.JSException) -> str:
    """First line of a QuickJS exception plus the script line it points at."""
    msg = str(exc).strip()
    first = msg.splitlines()[0] if msg else "Error"
    if "interrupted" in first:
        return "TimeoutError: script exceeded its CPU time limit"
    m = re.search(r"<input>:(\d+)", msg)
    return f"{first} (line {m.group(1)})" if m else first


async def run(
    code: str, host: HostCall, names: tuple[str, ...], limits: Limits = DEFAULT_LIMITS
) -> Outcome:
    out = Outcome()
    ctx = quickjs.Context()
    ctx.set_memory_limit(limits.memory_bytes)
    ctx.set_time_limit(limits.cpu_s)
    ctx.eval(_prelude(names, limits.log_chars))
    settle = ctx.get("__settle")
    tasks: dict[asyncio.Task, int] = {}
    backlog: list[list] = []

    def start_queued() -> None:
        backlog.extend(json.loads(ctx.eval("__drain()")))
        while backlog and len(tasks) < limits.max_inflight:
            call_id, name, args = backlog.pop(0)
            out.calls += 1
            if out.calls > limits.max_calls:
                raise _Abort(f"LimitError: more than {limits.max_calls} wire calls in one script")
            tasks[asyncio.create_task(host(name, args))] = call_id

    try:
        async with asyncio.timeout(limits.wall_s):
            # User code starts on line 1, so QuickJS line numbers match the script.
            ctx.eval("__finish((async () => {" + code + "\n})())")
            while True:
                while ctx.execute_pending_job():
                    pass
                start_queued()
                if state := ctx.eval("__state()"):
                    break
                if not tasks:
                    raise _Abort("Error: script awaited a promise that never settles")
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    call_id = tasks.pop(task)
                    try:
                        data, raw = task.result()
                        out.raw_tokens += raw
                        settle(call_id, True, json.dumps(data, ensure_ascii=False, default=str))
                    except Exception as e:  # noqa: BLE001 - any host failure rejects only that promise
                        settle(call_id, False, f"{type(e).__name__}: {e}")
        result = json.loads(state)
        out.text = result.get("text")
        if "error" in result:
            line = result.get("line")
            out.error = result["error"] + (f" (line {line})" if line else "")
    except TimeoutError:
        out.error = f"TimeoutError: script ran longer than {limits.wall_s:g}s"
    except quickjs.JSException as e:
        out.error = _js_error(e)
    except _Abort as e:
        out.error = str(e)
    finally:
        for task in tasks:
            task.cancel()
    try:
        out.logs = ctx.eval("__logs()")
    except quickjs.JSException:
        pass
    return out
