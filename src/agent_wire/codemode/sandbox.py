"""Run one async JavaScript snippet in QuickJS, with `wire.*` calls served by the host.

The script gets no filesystem, network, timers or modules. A `wire.x(...)` call only queues a
request in an outbox that the host drains between JS steps, so QuickJS never calls back into
Python (that keeps its CPU limit enforceable) and `Promise.all` fans calls out in parallel.
"""

import asyncio
import json
import re
import time
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


CPU_LIMIT = "TimeoutError: script exceeded its CPU time limit"


# The host's hooks live in a closure, handed to Python once and then deleted from the global
# scope, so a script can neither replace them nor settle its own calls. They use built-ins
# captured before the script runs, and every string that comes back to Python is JSON (QuickJS
# can't pass a lone surrogate to Python as a str; JSON escapes it).
_PRELUDE = r"""
(() => {
  const {stringify, parse} = JSON, apply = Reflect.apply, then = Promise.prototype.then;
  const P = Promise, Err = Error, freeze = Object.freeze;
  const pending = Object.create(null), logs = [];
  let queue = [], next = 0, logSize = 0, out = null;
  class WireError extends Error { constructor(m) { super(m); this.name = "WireError"; } }
  const call = (name, args) => new P((res, rej) => {
    const id = ++next; pending[id] = {res, rej}; queue[queue.length] = [id, name, args];
  });
  const show = x => { if (typeof x === "string") return x;
    try { return stringify(x); } catch (e) { return String(x); } };
  const log = (...a) => { if (logSize > LOG_CAP) return;
    const s = a.map(show).join(" "); logSize += s.length + 1; logs[logs.length] = s; };
  const line = e => { const m = /<input>:(\d+)/.exec((e && e.stack) || ""); return m ? +m[1] : null; };
  globalThis.console = freeze({log, info: log, warn: log, error: log, debug: log});
  globalThis.wire = freeze(Object.fromEntries(NAMES.map(n => [n, (...a) => call(n, a)])));
  globalThis.__hooks = {
    finish: f => apply(then, f(), [v => {
      if (typeof v === "string") { out = {text: v}; return; }
      try { out = {text: stringify(v === undefined ? null : v) ?? "null"}; }
      catch (e) { out = {error: "TypeError: return value is not JSON-serializable: " + e.message}; }
    }, e => {
      out = e instanceof Err
        ? {error: `${e.name}: ${e.message}`, line: e instanceof WireError ? null : line(e)}
        : {error: "Uncaught: " + show(e)};
    }]),
    drain: () => { const q = queue; queue = []; return stringify(q); },
    settle: (id, ok, payload) => {
      const p = pending[id]; delete pending[id];
      ok ? p.res(parse(payload)) : p.rej(new WireError(payload));
    },
    state: () => out === null ? "" : stringify(out),
    logs: () => stringify(logs),
  };
})();
"""
_HOOKS = ("finish", "drain", "settle", "state", "logs")


def _prelude(names: tuple[str, ...], log_chars: int) -> str:
    return _PRELUDE.replace("NAMES", json.dumps(list(names))).replace("LOG_CAP", str(log_chars))


_SURROGATE = re.compile("[\ud800-\udfff]")


def _clean(s: str) -> str:
    """Swap lone surrogates (legal in JS strings, not in UTF-8) for U+FFFD."""
    return _SURROGATE.sub("�", s)


def _js_error(exc: quickjs.JSException) -> str:
    """First line of a QuickJS exception plus the script line it points at."""
    msg = str(exc).strip()
    first = msg.splitlines()[0] if msg else "Error"
    if "interrupted" in first:
        return CPU_LIMIT
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
    hook = {name: ctx.eval(f"__hooks.{name}") for name in _HOOKS}
    ctx.eval("delete globalThis.__hooks")
    tasks: dict[asyncio.Task, int] = {}
    backlog: list[list] = []

    def start_queued() -> None:
        backlog.extend(json.loads(hook["drain"]()))
        while backlog and len(tasks) < limits.max_inflight:
            call_id, name, args = backlog.pop(0)
            out.calls += 1
            if out.calls > limits.max_calls:
                raise _Abort(f"LimitError: more than {limits.max_calls} wire calls in one script")
            tasks[asyncio.create_task(host(name, args))] = call_id

    try:
        async with asyncio.timeout(limits.wall_s):
            # User code starts on line 1, so QuickJS line numbers match the script.
            hook["finish"](ctx.eval("(async () => {" + _clean(code) + "\n})"))
            while True:
                # QuickJS's limit resets per job, so `for (;;) await 0` needs this deadline too;
                # without it the loop never yields and wall_s can't fire.
                deadline = time.monotonic() + limits.cpu_s
                while ctx.execute_pending_job():
                    if time.monotonic() > deadline:
                        raise _Abort(CPU_LIMIT)
                start_queued()
                if state := hook["state"]():
                    break
                if not tasks:
                    raise _Abort("Error: script awaited a promise that never settles")
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    call_id = tasks.pop(task)
                    try:
                        data, raw = task.result()
                        out.raw_tokens += raw
                        hook["settle"](call_id, True, json.dumps(data, default=str))
                    except Exception as e:  # noqa: BLE001 - any host failure rejects only that promise
                        hook["settle"](call_id, False, _clean(f"{type(e).__name__}: {e}"))
        result = json.loads(state)
        out.text = _clean(result["text"]) if result.get("text") is not None else None
        if "error" in result:
            line = result.get("line")
            out.error = _clean(result["error"]) + (f" (line {line})" if line else "")
    except TimeoutError:
        out.error = f"TimeoutError: script ran longer than {limits.wall_s:g}s"
    except quickjs.JSException as e:
        out.error = _js_error(e)
    except _Abort as e:
        out.error = str(e)
    except Exception as e:  # noqa: BLE001 - whatever the script did, wire_run answers, never raises
        out.error = f"Error: sandbox failed: {type(e).__name__}"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # let channel cleanup run now
    try:
        out.logs = "\n".join(_clean(s) for s in json.loads(hook["logs"]()))
    except Exception:  # noqa: BLE001, S110 - logs are best effort
        pass
    return out
