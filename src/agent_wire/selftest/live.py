"""`wire selftest --live`: one real request per channel, keyless Reddit, and the shield's
false-positive rate on the 16 public pages from the README benchmark. Needs network; never run in CI.
"""

# channel -> (op, argument). None: no stable public target to probe.
PROBES: dict[str, tuple[str, str] | None] = {
    "web": ("read", "https://example.com/"),
    "search": ("search", "model context protocol"),
    "github": ("read", "https://github.com/astral-sh/uv"),
    "reddit": ("feed", "r/Python"),
    "x": ("read", "https://x.com/jack/status/20"),
    "youtube": ("read", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    "hackernews": ("read", "https://news.ycombinator.com/item?id=1"),
    "stackoverflow": ("read", "https://stackoverflow.com/questions/231767"),
    "wikipedia": ("read", "https://en.wikipedia.org/wiki/Model_Context_Protocol"),
    "rss": ("feed", "https://hnrss.org/frontpage"),
    "arxiv": ("read", "https://arxiv.org/abs/1706.03762"),
    "linkedin": ("search", "python developer"),
    "hubs": ("read", "https://pypi.org/project/httpx/"),
    "markets": ("feed", "AAPL"),
    "meta_social": ("read", "https://www.instagram.com/natgeo/"),
    "publishing": ("feed", "@karpathy"),
    "fediverse": ("read", "https://bsky.app/profile/bsky.app"),
    "archive": ("search", "example.com"),
    "polymarket": ("search", "election"),
    "news": ("search", "artificial intelligence"),
    "stocktwits": ("feed", "$NVDA"),
    "devto": ("read", "https://dev.to/t/ai"),
    "bilibili": ("read", "https://www.bilibili.com/video/BV1xx411c7mD/"),
    "xiaohongshu": None,  # notes need a saved login and a current note id
    "wechat": ("search", "人工智能"),
    "zhihu": ("read", "https://www.zhihu.com/question/19550225"),
    "v2ex": ("read", "https://www.v2ex.com/t/1"),
    "douyin_tiktok": ("read", "https://www.tiktok.com/@scout2015/video/6718335390845095173"),
    "xiaoyuzhou": None,  # episode ids rotate; no stable public one to probe
    "xueqiu": ("read", "https://xueqiu.com/S/SH600519"),
    "boss_zhipin": ("read", "https://www.zhipin.com/web/geek/job?query=python"),
}

# Often behind a login wall, WAF or bot check without a saved session or the stealth browser.
SESSION_CHANNELS = {"linkedin", "meta_social", "xiaohongshu", "zhihu", "xueqiu", "boss_zhipin",
                    "wechat", "douyin_tiktok", "x"}  # fmt: skip

# The 16 pages behind the README's token benchmark (2026-11-04).
BENCHMARK_PAGES = [
    "https://github.com/astral-sh/uv/issues/1495",
    "https://github.com/astral-sh/uv",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://en.wikipedia.org/wiki/Model_Context_Protocol",
    "https://dev.to/t/ai",
    "https://blog.cloudflare.com/the-road-to-quic/",
    "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API",
    "https://bsky.app/profile/bsky.app",
    "https://news.ycombinator.com/item?id=1",
    "https://docs.python.org/3/library/asyncio.html",
    "https://arxiv.org/abs/1706.03762",
    "https://stackoverflow.com/questions/231767",
    "https://medium.com/@karpathy/software-2-0-a64152b37c35",
    "https://www.reddit.com/r/Python/",
    "https://www.npmjs.com/package/react",
    "https://nowsecure.nl/",
]


async def probe(wire, name: str) -> tuple[str, str]:
    """(status, detail) for one channel against the real site."""
    target = PROBES.get(name)
    if target is None:
        return "skip", "no stable public target"
    op, arg = target
    if op == "read":
        out = await wire.read(arg, max_tokens=600)
    elif op == "search":
        platform = "web" if name == "search" else name
        out = await wire.search(arg, platforms=[platform], limit=3, auto_expand_top=0,
                                max_tokens=600)  # fmt: skip
    else:
        out = await wire.feed(name, arg, limit=3, max_tokens=600)
    head = out.split("\n", 1)[0]
    if head.startswith("[WIRE | error") or len(out) < 120:
        reason = out.split("\n", 1)[-1].strip()[:160] or head
        if name in SESSION_CHANNELS:
            return "skip", f"needs a session or the stealth browser: {reason}"
        return "fail", reason
    if name == "reddit":
        from agent_wire.channels.tier1_global.reddit import ARCTIC_NOTE

        source = "Arctic Shift archive" if ARCTIC_NOTE in out else "reddit.com public JSON/RSS"
        return "ok", f"no key; answered by {source}"
    return "ok", head[:110]


async def false_positives(wire) -> tuple[int, int, list[str]]:
    """(pages read, pages with a shield alert, notes). The pages are benign: an alert is an FP."""
    led = wire.shield.ledger
    read, flagged, notes = 0, 0, []
    for url in BENCHMARK_PAGES:
        before = led.last_id()
        out = await wire.read(url, max_tokens=800)
        if out.startswith("[WIRE | error"):
            notes.append(f"unread {url}: {out.split(chr(10), 1)[-1][:100]}")
            continue
        read += 1
        alerts = [a for a in led.after(before) if (a.get("tier") or 0) >= 1]
        if alerts:
            flagged += 1
            notes.append(f"{url}: " + ", ".join(sorted({a["rule_id"] for a in alerts})))
    return read, flagged, notes
