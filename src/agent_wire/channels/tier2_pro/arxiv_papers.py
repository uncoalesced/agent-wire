"""arXiv: full paper as Markdown (arxiv.org/html, ar5iv), abstract fallback, Semantic Scholar stats."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlsplit

from curl_cffi.requests.exceptions import RequestException

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.channels.tier1_global.web import extract_article
from agent_wire.compression.anchor_slicer import slice_section
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.models import WireSearchHit

API = "https://export.arxiv.org/api/query"
_SUBMITTED = re.compile(r"Submitted\s+(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})")


def _words(node) -> str:
    return " ".join(node.text(separator=" ").split()) if node else ""


def parse_search_html(html: str) -> list[WireSearchHit]:
    """arxiv.org/search results (used when the export API times out)."""
    from datetime import UTC, datetime

    from selectolax.lexbor import LexborHTMLParser

    hits = []
    for li in LexborHTMLParser(html).css("li.arxiv-result"):
        a = li.css_first("p.list-title a")
        if not a or not (href := a.attributes.get("href")):
            continue
        when = None
        if m := _SUBMITTED.search(_words(li.css_first("p.is-size-7"))):
            try:
                when = datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%d %B %Y").replace(tzinfo=UTC)
            except ValueError:
                when = None
        hits.append(WireSearchHit(
            url=href, title=_words(li.css_first("p.title")), source="arxiv", timestamp=when,
            snippet=_words(li.css_first("span.abstract-full")).removesuffix("△ Less").strip()[:300],
            author=", ".join(_words(x) for x in li.css("p.authors a")[:3]),
        ))  # fmt: skip
    return hits


S2 = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{}?fields=title,year,venue,citationCount,tldr"
_ID = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)")
_ATOM = "{http://www.w3.org/2005/Atom}"


def arxiv_id(url: str) -> str | None:
    path = urlsplit(url).path
    m = _ID.search(path.removesuffix(".pdf"))
    return m[1] if m else None


def parse_atom(content: bytes) -> list[dict]:
    root = ET.fromstring(content)
    out = []
    for e in root.findall(f"{_ATOM}entry"):
        link = e.findtext(f"{_ATOM}id") or ""
        out.append({
            "id": link.rsplit("/abs/", 1)[-1],
            "title": " ".join((e.findtext(f"{_ATOM}title") or "").split()),
            "summary": " ".join((e.findtext(f"{_ATOM}summary") or "").split()),
            "authors": [a.findtext(f"{_ATOM}name") for a in e.findall(f"{_ATOM}author")],
            "published": ts(e.findtext(f"{_ATOM}published")),
            "url": link,
            "categories": [c.get("term") for c in e.findall(f"{_ATOM}category")],
        })  # fmt: skip
    return out


class ArxivChannel(Channel):
    name = "arxiv"

    async def _citations(self, pid: str) -> dict:
        try:
            data, _ = await self.fetch_json(S2.format(pid.split("v")[0]))
        except (WireFetchError, RequestException, ValueError):
            return {}
        tldr = (data.get("tldr") or {}).get("text")
        return {"citations": data.get("citationCount"), "venue": data.get("venue") or None,
                "tldr": tldr}  # fmt: skip

    async def read(self, url, *, query=None, section=None, include_comments=True):
        pid = arxiv_id(url)
        if not pid:
            raise WireUnsupported("arxiv: expected an /abs/, /pdf/ or /html/ paper URL")
        abs_data, abs_f = await self.fetch_json_atom(pid)
        meta = await self._citations(pid)
        try:  # full text: arXiv's own HTML render, ar5iv as mirror
            f = await self.fetch(f"https://arxiv.org/html/{pid}", archive=False, browser=False,
                                 mirrors=[f"https://ar5iv.labs.arxiv.org/html/{pid}"])  # fmt: skip
            body, _ = extract_article(f.text)
        except WireFetchError:
            f, body = abs_f, ""
        if not body.strip() and abs_data:
            body = f"## Abstract\n{abs_data['summary']}\n\n_Full text not available as HTML; abstract only._"
        if section:
            body = slice_section(body, section) or body
        if meta.get("tldr"):
            body = f"TL;DR: {meta['tldr']}\n\n{body}"
        return self.doc(
            f, f"https://arxiv.org/abs/{pid}", title=abs_data.get("title") if abs_data else pid,
            author_handle=", ".join(abs_data.get("authors", [])[:6]) if abs_data else None,
            timestamp=abs_data.get("published") if abs_data else None, body=body,
            metadata={"arxiv": pid, "categories": ", ".join(abs_data.get("categories", [])[:4]) if abs_data else None,
                      "citations": meta.get("citations"), "venue": meta.get("venue")},
        )  # fmt: skip

    async def fetch_json_atom(self, pid: str):
        f = await self.fetch(f"{API}?id_list={pid}", archive=False, browser=False)
        entries = parse_atom(f.content)
        return (entries[0] if entries else {}), f

    async def _query(self, q: str, limit: int, by_date: bool):
        sort = "submittedDate" if by_date else "relevance"
        f = await self.fetch(f"{API}?search_query={quote(q)}&max_results={limit}&sortBy={sort}&sortOrder=descending",
                             archive=False, browser=False)  # fmt: skip
        return parse_atom(f.content), f

    async def search(self, query, *, limit=5, sort="relevance"):
        try:
            entries, _ = await self._query(f"all:{query}", limit, sort in ("new", "date", "recent"))
        except WireFetchError:  # export.arxiv.org is slow / rate-limited at times
            try:
                return await self._html_search(query, limit, sort in ("new", "date", "recent"))
            except (WireFetchError, RequestException):
                return await self._s2_search(query, limit)
        return [WireSearchHit(url=e["url"], title=e["title"], snippet=e["summary"][:300], source="arxiv",
                              timestamp=e["published"], author=", ".join(a for a in e["authors"][:3] if a))
                for e in entries]  # fmt: skip

    async def _html_search(self, query: str, limit: int, by_date: bool) -> list[WireSearchHit]:
        order = "&order=-announced_date_first" if by_date else ""
        f = await self.fetch(f"https://arxiv.org/search/?query={quote(query)}&searchtype=all"
                             f"&size=25{order}", archive=False, browser=False)  # fmt: skip
        return parse_search_html(f.text)[:limit]

    async def _s2_search(self, query: str, limit: int) -> list[WireSearchHit]:
        data, _ = await self.fetch_json(
            f"https://api.semanticscholar.org/graph/v1/paper/search?query={quote(query)}&limit={limit}"
            "&fields=title,abstract,externalIds,year,citationCount"
        )
        hits = []
        for p in data.get("data", []):
            ax = (p.get("externalIds") or {}).get("ArXiv")
            url = (
                f"https://arxiv.org/abs/{ax}"
                if ax
                else f"https://www.semanticscholar.org/paper/{p['paperId']}"
            )
            hits.append(WireSearchHit(url=url, title=p.get("title") or "", source="semanticscholar",
                                      snippet=f"{p.get('year')} · {p.get('citationCount')} citations · "
                                              f"{(p.get('abstract') or '')[:240]}"))  # fmt: skip
        return hits

    async def feed(self, target, *, sort="hot", limit=10):
        cat = target.strip().removeprefix("arxiv:")
        entries, f = await self._query(f"cat:{cat}", limit, True)
        items = [{"title": e["title"], "url": e["url"], "meta": [", ".join(e["authors"][:3]),
                  e["published"].date().isoformat() if e["published"] else None],
                  "snippet": e["summary"]} for e in entries]  # fmt: skip
        return self.doc(
            f,
            f"https://arxiv.org/list/{cat}/recent",
            title=f"arXiv {cat} · newest",
            body=listing(items),
        )


CHANNEL = ArxivChannel
