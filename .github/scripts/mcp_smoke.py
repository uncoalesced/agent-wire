"""CI smoke test for an installed agent-wire: start the real stdio MCP server and talk to it.

No network: lists tools in both modes, runs a Code Mode script that makes no wire calls, and checks
that a runaway script comes back as an error instead of hanging the server.
"""

import asyncio
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

FULL = {"wire_read", "wire_search", "wire_feed", "wire_me", "wire_research", "wire_cite",
        "wire_run", "wire_status"}  # fmt: skip


async def check(args: list[str], expect: set[str]) -> None:
    transport = StdioTransport(sys.executable, ["-m", "agent_wire", *args])
    async with Client(transport) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == expect, f"{args}: tools {sorted(names)}"
        out = await c.call_tool("wire_run", {"code": "return [1 + 1, typeof wire.read]"})
        text = out.content[0].text
        assert text.startswith("[WIRE | code | 0 calls") and '[2,"function"]' in text, text
        hang = (await c.call_tool("wire_run", {"code": "for (;;) await 0"})).content[0].text
        assert hang.startswith("[WIRE | error | code") and "CPU time limit" in hang, hang
    print(f"ok: wire {' '.join(args)} -> {len(names)} tools")


async def main() -> None:
    await asyncio.wait_for(check(["serve"], FULL), 120)
    await asyncio.wait_for(check(["serve", "--code-mode"], {"wire_run", "wire_status"}), 120)


if __name__ == "__main__":
    asyncio.run(main())
