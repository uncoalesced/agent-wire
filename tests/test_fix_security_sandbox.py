"""Code Mode: busy loops stay off the event loop; console output is capped."""

import asyncio
import itertools
import time

from agent_wire.codemode import sandbox
from agent_wire.codemode.sandbox import Limits


async def host(name, args):
    return 1, 0


def test_busy_loop_does_not_freeze_event_loop():
    ticks = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def go():
        t = asyncio.create_task(ticker())
        out = await sandbox.run("for(;;){}", host, (), Limits(cpu_s=1.0))
        t.cancel()
        return out

    out = asyncio.run(go())
    assert "CPU time limit" in out.error
    gaps = [b - a for a, b in itertools.pairwise(ticks)]
    assert len(ticks) > 10 and max(gaps) < 0.5  # a frozen loop would show one ~1 s gap


def test_huge_console_log_is_capped():
    code = "console.log('x'.repeat(8_000_000)); console.log('y'.repeat(100)); return 1"
    out = asyncio.run(sandbox.run(code, host, (), Limits(log_chars=500)))
    assert out.text == "1" and len(out.logs) <= 500


def test_wire_calls_still_work_off_thread():
    async def fake(name, args):
        return {"n": args[0] * 2}, 5

    out = asyncio.run(sandbox.run("return await Promise.all([wire.x(1), wire.x(2)])", fake, ("x",)))
    assert out.text == '[{"n":2},{"n":4}]' and out.raw_tokens == 10
