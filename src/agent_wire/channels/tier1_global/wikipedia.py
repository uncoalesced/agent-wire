"""Wikipedia: clean plain-text extract (no [1] citations, no edit links) with section slicing."""

import re
from urllib.parse import quote, unquote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_text
from agent_wire.compression.anchor_slicer import slice_section
from agent_wire.models import WireSearchHit

_HEADING = re.compile(r"^(={2,6})[ \t]*(.*?)[ \t]*={2,6}[ \t]*$", re.MULTILINE)


def extract_to_markdown(extract: str) -> tuple[str, list[str]]:
    headings: list[str] = []

    def repl(m):
        level = len(m[1])
        if level == 2:
            headings.append(m[2])
        return "#" * level + " " + m[2]

    lines = _HEADING.sub(repl, extract).split("\n")
    kept = [ln for i, ln in enumerate(lines) if not _empty_section(lines, i)]
    md = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return md.strip(), [h for h in headings if f"## {h}" in md]


def _empty_section(lines: list[str], i: int) -> bool:
    """A heading followed only by blank lines and then a same-or-higher heading (or the end).

    MediaWiki leaves these for link-only sections like "See also"; a heading whose content is
    all in subsections is kept.
    """
    m = re.match(r"^(#+) ", lines[i])
    if not m:
        return False
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j == len(lines):
        return True
    nxt = re.match(r"^(#+) ", lines[j])
    return bool(nxt and len(nxt[1]) <= len(m[1]))


def parse_wiki_url(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = (parts.hostname or "en.wikipedia.org").replace(".m.wikipedia.org", ".wikipedia.org")
    if parts.path.startswith("/wiki/"):
        title = unquote(parts.path[len("/wiki/") :])
    elif "title=" in parts.query:
        title = unquote(re.search(r"title=([^&]+)", parts.query)[1])
    else:
        raise WireUnsupported("wikipedia: expected /wiki/<Title>")
    return host, title.replace("_", " ")


class WikipediaChannel(Channel):
    name = "wikipedia"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        host, title = parse_wiki_url(url)
        api = (f"https://{host}/w/api.php?action=query&prop=extracts|info&explaintext=1"
               f"&inprop=url&redirects=1&format=json&formatversion=2&titles={quote(title)}")  # fmt: skip
        data, f = await self.fetch_json(api)
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise WireUnsupported(f"no Wikipedia article {title!r} on {host}")
        page = pages[0]
        body, headings = extract_to_markdown(page.get("extract", ""))
        anchor = section or urlsplit(url).fragment
        if anchor and (part := slice_section(body, unquote(anchor).replace("_", " "))):
            body = part
        elif headings:
            body = f"Contents: {' · '.join(headings)}\n\n{body}"
        return self.doc(f, page.get("fullurl") or url, title=page.get("title"), body=body,
                        metadata={"lang": host.split(".")[0]})  # fmt: skip

    async def search(self, query, *, limit=5, sort="relevance"):
        data, _ = await self.fetch_json(
            "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
            f"&formatversion=2&srlimit={limit}&srsearch={quote(query)}"
        )
        return [
            WireSearchHit(url=f"https://en.wikipedia.org/wiki/{quote(r['title'].replace(' ', '_'))}",
                          title=r["title"], snippet=html_text(r.get("snippet", "")), source="wikipedia")
            for r in data.get("query", {}).get("search", [])
        ]  # fmt: skip


CHANNEL = WikipediaChannel
