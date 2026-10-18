"""Render a ResearchResult as a cited Markdown brief, JSON, or a standalone HTML page."""

import html
import json
import re
from datetime import UTC, datetime

from agent_wire.compression.dam_formatter import relative_age
from agent_wire.research.engine import ResearchResult

MAX_CLUSTERS = {"quick": 8, "default": 12, "deep": 20}
_ENG_LABEL = (("score", "▲"), ("likes", "♥"), ("reposts", "↻"), ("comments", "💬"), ("stars", "★"),
              ("views", "views "), ("volume24h", "$24h "))  # fmt: skip


def _num(n: int) -> str:
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def engagement_line(eng: dict[str, int]) -> str:
    return " ".join(f"{label}{_num(eng[k])}" for k, label in _ENG_LABEL if eng.get(k))


def title_of(r: ResearchResult) -> str:
    p = r.plan
    if p.mode == "discover":
        return f"What's trending · last {p.days} days"
    if p.mode == "person":
        return f"{p.github_user} on GitHub · last {p.days} days"
    return f"{p.topic} · last {p.days} days"


def headline(r: ResearchResult) -> str:
    if r.topics:
        return ", ".join(t["topic"] for t in r.topics[:3])
    return r.clusters[0].title if r.clusters else "no stories"


def _take_line(t) -> str:
    who = f"@{t.comment.author_handle}" if t.comment.author_handle else "someone"
    unit = "replies" if t.comment.score_kind == "replies" else "pts"
    score = f" ({t.comment.score} {unit})" if t.comment.score else ""
    return f"   > {who}{score}: {' '.join(t.comment.body.split())[:240]}"


def to_markdown(r: ResearchResult, max_clusters: int | None = None) -> str:
    p = r.plan
    now = p.until
    n = max_clusters or MAX_CLUSTERS[p.depth]
    out = [f"# {title_of(r)}", ""]
    srcs = ", ".join(f"{s} {c}" for s, c in sorted(r.counts.items(), key=lambda kv: -kv[1]))
    if p.mode == "person":
        out.append(f"Window {p.since.date()} → {p.until.date()}")
    else:
        out.append(f"Window {p.since.date()} → {p.until.date()} · {len(r.items)} items in "
                   f"{len(r.clusters)} stories · {srcs or 'no sources answered'}")  # fmt: skip

    if r.topics:
        out += ["", "## Trending topics"]
        for i, t in enumerate(r.topics, 1):
            ex = " · ".join(f"[{e['title'][:70]}]({e['url']})" for e in t["examples"])
            out.append(f"{i}. **{t['topic']}** · {t['momentum']} · {t['mentions']} stories on "
                       f"{', '.join(t['sources'])}\n   {ex}")  # fmt: skip

    if r.subjects:
        out += ["", "## Head to head", "", "| | items | heat | sources | top story |",
                "|---|---|---|---|---|"]  # fmt: skip
        for s in r.subjects:
            top = f"[{s['top']['title'][:60]}]({s['top']['url']})" if s["top"] else "-"
            out.append(f"| **{s['subject']}** | {s['items']} | {_num(s['heat'])} | "
                       f"{', '.join(s['sources']) or '-'} | {top} |")  # fmt: skip
        if both := max((s["head_to_head"] for s in r.subjects), default=0):
            out.append(f"\n{both} items discuss them side by side.")

    if r.person:
        out += ["", "## GitHub activity", r.person]

    takes: dict[int, list] = {}
    for t in r.takes:
        takes.setdefault(t.cluster, []).append(t)
    if r.clusters and p.mode != "person":
        out += ["", "## Biggest stories" if p.mode == "discover" else "## Top stories"]
        for idx, c in enumerate(r.clusters[:n]):
            when = relative_age(c.first_seen, now) if c.first_seen else "undated"
            bits = (", ".join(c.sources), engagement_line(c.engagement), when, c.momentum(now))
            out.append(
                f"{idx + 1}. **[{c.title}]({c.lead.hit.url})** · "
                + " · ".join(b for b in bits if b)
            )
            seen = {c.lead.hit.url}
            cites = []
            for i in sorted(c.items, key=lambda i: -i.score):
                if i.hit.url not in seen and len(cites) < 4:
                    seen.add(i.hit.url)
                    cites.append(f"[{i.source}]({i.hit.url})")
            if c.link and c.link not in seen:
                cites.append(f"[article]({c.link})")
            if cites:
                out.append("   also: " + " · ".join(cites))
            # the platform's own blurb beats the start of a page (a repo's file list, a nav bar)
            if snippet := (c.lead.hit.snippet or r.excerpts.get(idx) or "").strip():
                out.append("   " + " ".join(snippet.split())[:280])
            out += [_take_line(t) for t in takes.get(idx, [])]

    markets = r.markets
    if markets:
        out += ["", "## Where the money is" if p.mode == "discover" else "## What the markets say"]
        out += [f"- [{i.hit.title}]({i.hit.url}) · {i.hit.snippet} · {engagement_line(i.hit.engagement)}"
                for i in markets[:4]]  # fmt: skip

    if r.library:
        out += ["", "## From your library"]
        out += [f"- {x['created_at'][:10]} · run #{x['id']} · {x['topic']}: {x['headline']}"
                for x in r.library]  # fmt: skip

    if r.gaps or p.notes:
        out += ["", "## Not covered"]
        out += [f"- {s}: {why}" for s, why in sorted(r.gaps.items())]
        out += [f"- {note}" for note in p.notes]
    return "\n".join(out).strip()


def to_dict(r: ResearchResult) -> dict:
    p = r.plan

    def item(i):
        h = i.hit
        return {"url": h.url, "title": h.title, "source": h.source, "author": h.author, "link": h.link,
                "timestamp": h.timestamp.isoformat() if h.timestamp else None,
                "engagement": h.engagement, "score": i.score, "relevance": round(i.relevance, 3),
                "freshness": round(i.freshness, 3), "snippet": h.snippet[:300]}  # fmt: skip

    return {
        "topic": p.topic, "mode": p.mode, "depth": p.depth, "days": p.days,
        "window": {"since": p.since.isoformat(), "until": p.until.isoformat()},
        "created_at": r.created_at.isoformat(), "elapsed_ms": r.elapsed_ms, "run_id": r.run_id,
        "counts": r.counts, "gaps": r.gaps, "subjects": r.subjects, "topics": r.topics,
        "person": r.person, "library": r.library, "headline": headline(r),
        "markets": [item(i) for i in r.markets],
        "clusters": [{"title": c.title, "score": c.score, "momentum": c.momentum(p.until),
                      "sources": c.sources, "engagement": c.engagement, "link": c.link,
                      "excerpt": r.excerpts.get(idx),
                      "takes": [{"author": t.comment.author_handle, "score": t.comment.score,
                                 "body": t.comment.body[:600], "url": t.url}
                                for t in r.takes if t.cluster == idx],
                      "items": [item(i) for i in c.items]}
                     for idx, c in enumerate(r.clusters)],
    }  # fmt: skip


def to_json(r: ResearchResult) -> str:
    return json.dumps(to_dict(r), ensure_ascii=False, indent=2)


PAGE_CSS = """
:root{--bg:#fbfaf7;--fg:#1c1b19;--muted:#6b6862;--line:#e4e1da;--accent:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#9c998f;--line:#2c2b28;--accent:#4fc39a}}
body{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif;margin:0 auto;max-width:860px;padding:24px 16px}
a{color:var(--accent)}h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.15rem;margin-top:2em;border-bottom:1px solid var(--line)}
blockquote{margin:.3em 0 .3em 1em;padding-left:.8em;border-left:3px solid var(--line);color:var(--muted)}
.meta{color:var(--muted);font-size:.9rem}table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}
td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}
"""
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _LINK.sub(
        lambda m: f'<a href="{html.escape(m[2])}" rel="noopener nofollow">{m[1]}</a>', text
    )


def md_to_html(md: str) -> str:
    """Renderer for the brief's own Markdown subset (headings, quotes, links, bold, tables)."""
    out, in_table = [], False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("|"):
            if set(s) <= set("|-: "):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
            out.append(
                "<tr>"
                + "".join(f"<td>{_inline(c.strip())}</td>" for c in s.strip("|").split("|"))
                + "</tr>"
            )
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if s.startswith("## "):
            out.append(f"<h2>{_inline(s[3:])}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{_inline(s[2:])}</h1>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{_inline(s[2:])}</blockquote>")
        elif s:
            out.append(f"<p>{_inline(s)}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def page(markdown: str, title: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body>"
            f"{md_to_html(markdown)}<p class=meta>Generated by agent-wire · {stamp}</p></body></html>")  # fmt: skip


def render(r: ResearchResult, fmt: str = "md") -> str:
    if fmt == "json":
        return to_json(r)
    if fmt == "html":
        return page(to_markdown(r), title_of(r))
    if fmt != "md":
        raise ValueError("emit must be md, json or html")
    return to_markdown(r)
