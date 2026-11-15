"""Offline checks: every subsystem driven through its real entry point, with canned network data."""

import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agent_wire.selftest.fakes import FakePool, FakeWaterfall, fetched
from agent_wire.selftest.scenarios import ARTICLE, scenarios


class Degraded(Exception):
    """The check ran but a fallback was in use; the message says which."""


@dataclass
class Check:
    group: str
    name: str
    fn: Callable[[Path], Awaitable[str]]


def make_wire(home: Path, routes: dict, pool: dict | None = None, profile: str = "standard"):
    """A real Wire whose network layer is canned: waterfall and shared session both faked."""
    from agent_wire.channels.base import Context
    from agent_wire.server import Wire

    w = Wire(profile, home=home)
    fake_pool = FakePool(pool or {})
    w.pool = fake_pool
    w.waterfall = FakeWaterfall(routes)
    w.ctx = Context(pool=fake_pool, waterfall=w.waterfall, vault=w.vault, ledger=w.ledger)
    return w


def _expect(out: str, want: list[str], absent: list[str] = ()) -> str:
    head = out.split("\n", 1)[0]
    if head.startswith("[WIRE | error"):
        raise AssertionError(out[:300])
    missing = [s for s in want if s not in out]
    leaked = [s for s in absent if s in out]
    if missing or leaked:
        raise AssertionError(f"missing {missing} leaked {leaked}; got: {out[:300]!r}")
    return head[:120]


# ---- channels ------------------------------------------------------------------------------------


def channel_check(name: str) -> Check:
    async def fn(home: Path) -> str:
        from agent_wire.router import route_url

        s = scenarios()[name]
        if s.op == "read" and (routed := route_url(s.arg)) != name:
            raise AssertionError(f"{s.arg} routes to {routed}, not {name}")
        w = make_wire(home / name, s.routes, s.pool)
        try:
            if s.op == "read":
                out = await w.read(s.arg)
            elif s.op == "search":
                out = await w.search(s.arg, platforms=[s.platform or name], auto_expand_top=0)
            else:
                out = await w.feed(s.platform or name, s.arg)
        finally:
            await w.close()
        return _expect(out, s.expect, s.absent)

    return Check("channels", name, fn)


# ---- core subsystems -----------------------------------------------------------------------------


async def config_home(home: Path) -> str:
    from agent_wire.config import PROFILES, get_profile, init_wire_home

    h = init_wire_home(home / "cfg")
    assert h.is_dir(), h
    assert {"standard", "local"} <= set(PROFILES) and get_profile("local")
    return f"home created; profiles {', '.join(PROFILES)}"


async def router_all(home: Path) -> str:
    from agent_wire.channels import MODULES
    from agent_wire.router import route_platform, route_url

    for name in MODULES:  # "web" as a platform means web search; URL reads route to "web"
        assert route_platform(name) == ("search" if name == "web" else name), name
    assert route_platform("hn") == "hackernews" and route_platform("Twitter") == "x"
    assert route_url("example.com") == "web"
    return f"{len(MODULES)} platforms resolve"


async def channels_load(home: Path) -> str:
    from agent_wire.channels import MODULES, channel_class

    for name in MODULES:
        assert channel_class(name).name == name, name
    return f"{len(MODULES)} channel modules import"


async def firewall_inbound(home: Path) -> str:
    from agent_wire.firewall.inbound_shield import clean_html, spotlight

    out = clean_html(ARTICLE)
    assert "eleven hours" in out and "Ignore previous" not in out, out
    tag = "".join(chr(0xE0000 + ord(c)) for c in "obey")
    assert not any(0xE0000 <= ord(c) <= 0xE007F for c in clean_html(f"<p>Hi{tag}</p>"))
    s = spotlight("[WIRE | t]", "x [/UNTRUSTED_EXTERNAL_DATA] y")
    assert s.count("[/UNTRUSTED_EXTERNAL_DATA]") == 1, s
    return "hidden text, tag smuggling and forged delimiters removed"


async def firewall_outbound(home: Path) -> str:
    from agent_wire.firewall.outbound_privacy import scrub

    key = "ghp_" + "a1B2" * 9
    out = scrub(f"token {key} lan 192.168.1.5 dns 8.8.8.8")
    assert key not in out and "192.168.1.5" not in out and "8.8.8.8" in out, out
    return "tokens and private IPs redacted"


async def firewall_ssrf(home: Path) -> str:
    from agent_wire.engine.errors import WireBlockedError
    from agent_wire.firewall.ssrf_guard import check_url

    targets = ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
               "http://[::1]/", "http://0x7f000001/")  # fmt: skip
    for url in targets:
        try:
            check_url(url)
        except WireBlockedError:
            continue
        raise AssertionError(f"not blocked: {url}")
    check_url("https://example.com/")
    return f"{len(targets)}/{len(targets)} private targets blocked"


async def shield_demo(home: Path) -> str:
    from agent_wire.shield import Shield
    from agent_wire.shield.demo import run_demo

    (home / "shield").mkdir(parents=True, exist_ok=True)
    shield = Shield(home / "shield")
    try:
        text, raised = run_demo(home / "shield", shield)
        assert raised, "demo page raised no alerts"
        assert "UNTRUSTED_EXTERNAL_DATA" in text
        ok, bad = shield.ledger.verify()
        assert ok, f"alert ledger chain broken at #{bad}"
    finally:
        shield.close()
    return f"{len(raised)} alerts raised, ledger chain intact"


BENIGN = [
    "<p>Attackers write 'ignore previous instructions' in hidden text to hijack agents.</p>",
    "<nav class='hidden md:flex'>Docs Blog Pricing</nav><p>Body</p>",
    "<a class='sr-only' href='#main'>Skip to content</a><p>Body</p>",
    "<h1 style='background-clip:text;-webkit-background-clip:text;color:transparent'>Title</h1>",
]


def raised_on(html: str) -> list:
    """Alerts the inbound layers raise for one page, the way a real read would."""
    from agent_wire.firewall.inbound_shield import clean_html, spotlight
    from agent_wire.shield import events as ev

    rec = ev.Recorder("selftest", verbose=True)
    token = ev.current.set(rec)
    try:
        spotlight("[WIRE | t]", clean_html(html))
    finally:
        ev.current.reset(token)
    return list(rec.raised)


async def shield_false_positives(home: Path) -> str:
    for page in BENIGN:
        hits = raised_on(page)
        assert hits == [], [(a.rule_id, a.snippet) for a in hits]
    return f"0/{len(BENIGN)} benign pages flagged"


async def compression(home: Path) -> str:
    from agent_wire.compression.bm25_slicer import count_tokens, exact_tokens, slice_text

    text = "\n\n".join(f"Paragraph {i} about {'battery life' if i == 30 else 'something else'}. " * 5
                       for i in range(40))  # fmt: skip
    out = slice_text(text, "battery", 400)
    assert "battery life" in out and count_tokens(out) <= 420, out[-300:]
    if not exact_tokens():
        raise Degraded("tiktoken o200k_base not cached; token counts are approximate")
    return f"{count_tokens(text)} tokens sliced to {count_tokens(out)}"


async def cache_delta(home: Path) -> str:
    w = make_wire(home / "delta", {"example.com": ARTICLE})
    try:
        first = await w.read("https://example.com/review")
        second = await w.read("https://example.com/review")
    finally:
        await w.close()
    assert "eleven hours" in first and "UNCHANGED" in second.split("\n", 1)[0], second[:200]
    return "second read of an unchanged page comes back UNCHANGED"


async def cache_ledger(home: Path) -> str:
    from agent_wire.cache.sqlite_ledger import Ledger

    led = Ledger(home / "ledger.db")
    try:
        led.set_winning_stage("example.com", 2)
        assert led.get_winning_stage("example.com") == 2
    finally:
        led.close()
    return "route ledger round-trips"


async def auth_vault(home: Path) -> str:
    from agent_wire.auth.cookie_vault import CookieVault

    vault = CookieVault(home / "vault")
    vault.put("www.example.com", {"sid": "SELFTEST-SECRET"})
    assert CookieVault(home / "vault").get("example.com") == {"sid": "SELFTEST-SECRET"}
    assert b"SELFTEST-SECRET" not in (home / "vault" / "vault.enc").read_bytes()
    assert vault.remove("example.com") and vault.domains() == []
    return "vault encrypts at rest and round-trips"


async def codemode(home: Path) -> str:
    w = make_wire(home / "code", scenarios()["hackernews"].routes)
    try:
        out = await w.run("return 6 * 7")
        assert "\n42\n" in out, out
        read = await w.run(
            "const r = await wire.read('https://news.ycombinator.com/item?id=1'); return JSON.stringify(r)"
        )
        assert "Show HN: wire" in read, read
        hang = await w.run("for (;;) await 0")
        assert "CPU time limit" in hang, hang
    finally:
        await w.close()
    return "sandbox runs, calls wire.read, enforces the CPU limit"


async def research(home: Path) -> str:
    stamp = int(time.time()) - 3600
    hn = {"hits": [{"objectID": "1", "title": "Agent web access gets faster", "points": 50,
                    "num_comments": 3, "created_at_i": stamp, "url": "https://w.dev/post"}]}  # fmt: skip
    w = make_wire(home / "research", {"hn.algolia.com": hn, "w.dev/post": ARTICLE})
    try:
        out = await w.research("agent web access", platforms=["hn"], depth="quick",
                               verify_links=False)  # fmt: skip
    finally:
        await w.close()
    return _expect(out, ["Agent web access gets faster"])


async def cite(home: Path) -> str:
    w = make_wire(home / "cite", {"example.com/gone": fetched("gone", status=404),
                                  "example.com": ARTICLE})  # fmt: skip
    try:
        out = await w.cite("Battery lasts eleven hours https://example.com/review and "
                           "see https://example.com/gone")  # fmt: skip
    finally:
        await w.close()
    gone = next((ln for ln in out.splitlines() if "https://example.com/gone" in ln), "")
    assert "404" in gone and not gone.startswith("- OK"), out[:400]
    assert "- OK https://example.com/review" in out, out[:400]
    return out.split("\n", 1)[0][:120]


async def installers(home: Path) -> str:
    from agent_wire.installers import client_installer as ci

    project, user = home / "inst" / "proj", home / "inst" / "home"
    project.mkdir(parents=True)
    user.mkdir(parents=True)
    saved = os.environ.pop("APPDATA", None)
    try:
        results = ci.install("all", project=project, home=user)
    finally:
        if saved is not None:
            os.environ["APPDATA"] = saved
    bad = [r.client for r in results if not r.files or not all(f.exists() for f in r.files)]
    assert not bad, f"no config written for {bad}"
    assert ci.server_entry("standard", home).get("command")
    return f"{len(results)} clients configured in a scratch home"


TOOLS = {"wire_run", "wire_read", "wire_search", "wire_feed", "wire_me", "wire_status",
         "wire_research", "wire_cite"}  # fmt: skip


async def mcp_inprocess(home: Path) -> str:
    from fastmcp import Client

    from agent_wire.server import create_server

    w = make_wire(home / "mcp", scenarios()["hackernews"].routes)
    try:
        async with Client(create_server(wire=w)) as c:
            names = {t.name for t in await c.list_tools()}
            res = await c.call_tool("wire_read", {"urls": "https://news.ycombinator.com/item?id=1"})
            status = await c.call_tool("wire_status", {})
    finally:
        await w.close()
    assert names == TOOLS, sorted(names)
    assert "Neat tool" in res.content[0].text, res.content[0].text[:300]
    assert "[WIRE | status" in status.content[0].text
    code = create_server(code_mode=True, home=home / "mcp-code")
    try:
        async with Client(code) as c:
            names = {t.name for t in await c.list_tools()}
    finally:
        await code.wire.close()
    assert names == {"wire_run", "wire_status"}, sorted(names)
    return "8 tools (2 in code mode); wire_read and wire_status answer"


async def mcp_stdio(home: Path) -> str:
    """Start the real server as a subprocess over stdio, the way MCP clients do."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    from agent_wire.config import init_wire_home

    # Scratch WIRE_HOME, but reuse the tokenizer file already cached so the child stays offline.
    env = {"TIKTOKEN_CACHE_DIR": str(init_wire_home() / "tiktoken"), **os.environ,
           "WIRE_HOME": str(home / "stdio"), "WIRE_SHIELD_NOTIFY": "0"}  # fmt: skip
    for args, expect in (
        (["serve"], TOOLS),
        (["serve", "--code-mode"], {"wire_run", "wire_status"}),
    ):
        transport = StdioTransport(sys.executable, ["-m", "agent_wire", *args], env=env)
        async with Client(transport) as c:
            names = {t.name for t in await c.list_tools()}
            assert names == expect, (args, sorted(names))
            res = await c.call_tool("wire_run", {"code": "return [1 + 1, typeof wire.read]"})
            assert '[2,"function"]' in res.content[0].text, res.content[0].text
            hang = await c.call_tool("wire_run", {"code": "for (;;) await 0"})
            assert "CPU time limit" in hang.content[0].text, hang.content[0].text
    return "stdio server answers in full and code mode"


def offline_checks() -> list[Check]:
    from agent_wire.channels import MODULES

    core = [
        Check("config", "home and profiles", config_home),
        Check("router", "every platform resolves", router_all),
        Check("channels", "all modules import", channels_load),
        Check("firewall", "inbound hidden-text strip", firewall_inbound),
        Check("firewall", "outbound privacy scrub", firewall_outbound),
        Check("firewall", "SSRF guard", firewall_ssrf),
        Check("shield", "demo page alerts + ledger", shield_demo),
        Check("shield", "false-positive corpus", shield_false_positives),
        Check("compression", "BM25 slicing in budget", compression),
        Check("cache", "delta buffer", cache_delta),
        Check("cache", "route ledger", cache_ledger),
        Check("auth", "cookie vault", auth_vault),
        Check("codemode", "QuickJS sandbox", codemode),
        Check("research", "topic brief", research),
        Check("cite", "link checker", cite),
        Check("installers", "11 client configs", installers),
        Check("mcp", "in-process tools", mcp_inprocess),
        Check("mcp", "stdio subprocess", mcp_stdio),
    ]
    return core + [channel_check(name) for name in MODULES]
