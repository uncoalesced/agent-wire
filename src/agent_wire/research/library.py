"""Your research library: offline search over saved briefs, plus a local site and Atom feed."""

import html
import xml.etree.ElementTree as ET
from pathlib import Path

from agent_wire.research.brief import page
from agent_wire.research.store import Store

ATOM = "http://www.w3.org/2005/Atom"
EPOCH = "1970-01-01T00:00:00+00:00"


class Library:
    def __init__(self, store: Store, home: Path):
        self.store = store
        self.dir = Path(home) / "library"

    def related(self, topic: str, limit: int = 3, exclude: int | None = None) -> list[dict]:
        return self.store.search(topic, limit=limit, exclude=exclude)

    def search_markdown(self, query: str, limit: int = 10) -> str:
        hits = self.store.search(query, limit=limit)
        if not hits:
            return f"no saved briefs match {query!r} (save runs with store=True / --store)"
        return "\n".join(f"{i}. run #{h['id']} · {h['created_at'][:10]} · {h['topic']} ({h['mode']}, "
                         f"{h['days']}d) · {h['headline']}" for i, h in enumerate(hits, 1))  # fmt: skip

    def build(self, limit: int = 200) -> Path:
        """Write index.html, feed.xml and one page per saved run under WIRE_HOME/library."""
        runs_dir = self.dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        runs = self.store.runs(limit=limit)
        for r in runs:
            full = self.store.run(r["id"])
            (runs_dir / f"{r['id']}.html").write_text(
                page(full["markdown"], f"#{r['id']} · {r['topic']}"), encoding="utf-8"
            )
        rows = "".join(
            f"<tr><td>{r['created_at'][:10]}</td><td><a href='runs/{r['id']}.html'>"
            f"{html.escape(r['topic'])}</a></td><td>{r['mode']}</td><td>{r['days']}d</td>"
            f"<td>{html.escape(r['headline'])}</td></tr>"
            for r in runs
        )
        table = ("<p><a href='feed.xml'>Atom feed</a></p><table><tr><th>date</th><th>topic</th>"
                 f"<th>mode</th><th>window</th><th>headline</th></tr>{rows}</table>")  # fmt: skip
        index = page("# Research library", "agent-wire library").replace(
            "<h1>Research library</h1>", "<h1>Research library</h1>" + table
        )
        (self.dir / "index.html").write_text(index, encoding="utf-8")
        (self.dir / "feed.xml").write_bytes(self.atom(runs))
        return self.dir

    def atom(self, runs: list[dict]) -> bytes:
        ET.register_namespace("", ATOM)

        def sub(parent, tag, text=None, **attrs):
            el = ET.SubElement(parent, f"{{{ATOM}}}{tag}", **attrs)
            el.text = text
            return el

        feed = ET.Element(f"{{{ATOM}}}feed")
        sub(feed, "title", "agent-wire research library")
        sub(feed, "id", "urn:agent-wire:library")
        sub(feed, "updated", runs[0]["created_at"] if runs else EPOCH)
        sub(sub(feed, "author"), "name", "agent-wire")
        for r in runs:
            e = sub(feed, "entry")
            sub(e, "title", f"{r['topic']} · last {r['days']} days")
            sub(e, "id", f"urn:agent-wire:run:{r['id']}")
            sub(e, "updated", r["created_at"])
            sub(e, "link", href=(self.dir / "runs" / f"{r['id']}.html").as_uri())
            sub(e, "summary", r["headline"])
        return ET.tostring(feed, encoding="utf-8", xml_declaration=True)
