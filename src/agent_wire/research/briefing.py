"""Daily / weekly digest across every watched topic, plus what is trending overall."""

from datetime import UTC, datetime

from agent_wire.research.brief import engagement_line

PERIODS = {"daily": 1, "weekly": 7}
TOP_PER_TOPIC = 5


async def digest(wire, period: str = "daily", *, trending: bool = True, save: bool = True) -> str:
    if period not in PERIODS:
        raise ValueError("period must be daily or weekly")
    days = PERIODS[period]
    stamp = datetime.now(UTC)
    out = [f"# {period.title()} briefing · {stamp.date()}"]
    watches = wire.store.watches()
    if not watches and not trending:
        return "no watched topics (wire watch add <topic>)"
    for w in watches:
        r, _, _ = await wire.research_result(w["topic"], days=days, platforms=w.get("sources"),
                                             depth="quick", store=True)  # fmt: skip
        out += ["", f"## {w['topic']}"]
        if not r.clusters:
            out.append("- nothing in this window")
        for c in r.clusters[:TOP_PER_TOPIC]:
            out.append(f"- [{c.title}]({c.lead.hit.url}) · {', '.join(c.sources)} · "
                       f"{engagement_line(c.engagement)} · {c.momentum(r.plan.until)}")  # fmt: skip
    if trending:
        r, _, _ = await wire.research_result("", days=days, depth="quick", store=False)
        out += ["", "## Trending everywhere"]
        out += [f"- **{t['topic']}** · {t['momentum']} · {', '.join(t['sources'])}"
                for t in r.topics[:8]]  # fmt: skip
    text = "\n".join(out)
    if save:
        folder = wire.library.dir / "briefings"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stamp.date()}-{period}.md"
        path.write_text(text, encoding="utf-8")
        text += f"\n\n_saved to {path}_"
    return text
