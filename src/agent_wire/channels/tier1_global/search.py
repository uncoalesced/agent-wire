"""Concurrent meta-search: DuckDuckGo + Brave (+ SearXNG / Exa when configured), consensus-ranked."""

import asyncio
import os
from urllib.parse import parse_qs, quote_plus, urlsplit

from curl_cffi.requests.exceptions import RequestException
from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireSearchHit

ENGINE_TIMEOUT_S = 8


def url_key(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}{'?' + p.query if p.query else ''}"


def parse_ddg_html(html: str) -> list[WireSearchHit]:
    hits = []
    for res in LexborHTMLParser(html).css("div.result"):
        if "result--ad" in (res.attributes.get("class") or ""):
            continue
        a = res.css_first("a.result__a")
        if not a:
            continue
        href = a.attributes.get("href") or ""
        if "uddg=" in href:
            href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
        if not href.startswith("http"):
            continue
        snip = res.css_first(".result__snippet")
        hits.append(WireSearchHit(url=href, title=a.text(strip=True),
                                  snippet=snip.text(strip=True) if snip else "", source="ddg"))  # fmt: skip
    return hits


def parse_ddg_lite(html: str) -> list[WireSearchHit]:
    tree = LexborHTMLParser(html)
    links = tree.css("a.result-link")
    snippets = tree.css("td.result-snippet")
    hits = []
    for i, a in enumerate(links):
        href = a.attributes.get("href") or ""
        if "uddg=" in href:
            href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
        if href.startswith("http"):
            snip = snippets[i].text(strip=True) if i < len(snippets) else ""
            hits.append(
                WireSearchHit(url=href, title=a.text(strip=True), snippet=snip, source="ddg")
            )
    return hits


def parse_brave(html: str) -> list[WireSearchHit]:
    hits = []
    for res in LexborHTMLParser(html).css('div.snippet[data-type="web"], #results .snippet'):
        a = res.css_first("a[href^='http']")
        if not a:
            continue
        title = res.css_first(".title, .snippet-title, .search-snippet-title")
        desc = res.css_first(".snippet-description, .generic-snippet .content, .snippet-content")
        hits.append(
            WireSearchHit(
                url=a.attributes["href"],
                title=(title or a).text(strip=True),
                snippet=desc.text(strip=True) if desc else "",
                source="brave",
            )
        )
    return hits


def fuse(result_lists: list[list[WireSearchHit]], limit: int) -> list[WireSearchHit]:
    """Reciprocal-rank consensus: a URL several engines agree on beats one engine's #1."""
    merged: dict[str, WireSearchHit] = {}
    for hits in result_lists:
        seen = set()
        for rank, h in enumerate(hits):
            key = url_key(h.url)
            if key in seen:
                continue
            seen.add(key)
            if key in merged:
                m = merged[key]
                m.rank_score += 1 / (rank + 1)
                if h.source not in m.source.split("+"):
                    m.source += f"+{h.source}"
                if len(h.snippet) > len(m.snippet):
                    m.snippet = h.snippet
            else:
                merged[key] = h.model_copy(update={"rank_score": 1 / (rank + 1)})
    return sorted(merged.values(), key=lambda h: -h.rank_score)[:limit]


class SearchChannel(Channel):
    name = "search"

    async def _html(self, url: str) -> str:
        f = await self.fetch(url, archive=False, browser=False)
        return f.text

    async def _ddg(self, query: str) -> list[WireSearchHit]:
        hits = parse_ddg_html(
            await self._html(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
        )
        return hits or parse_ddg_lite(
            await self._html(f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}")
        )

    async def _brave(self, query: str) -> list[WireSearchHit]:
        return parse_brave(
            await self._html(f"https://search.brave.com/search?q={quote_plus(query)}&source=web")
        )

    async def _searxng(self, query: str) -> list[WireSearchHit]:
        base = os.environ["WIRE_SEARXNG_URL"].rstrip("/")
        data, _ = await self.fetch_json(f"{base}/search?q={quote_plus(query)}&format=json")
        return [WireSearchHit(url=r["url"], title=r.get("title", ""), snippet=r.get("content", ""),
                              source="searxng") for r in data.get("results", [])]  # fmt: skip

    async def _exa(self, query: str) -> list[WireSearchHit]:
        f = await self.ctx.pool.get(
            "https://api.exa.ai/search",
            method="POST",
            impersonate=None,
            headers={"x-api-key": os.environ["EXA_API_KEY"], "content-type": "application/json"},
            json_body={"query": query, "numResults": 10, "contents": {"highlights": True}},
        )
        data = parse_json(f.content)
        return [
            WireSearchHit(url=r["url"], title=r.get("title") or r["url"],
                          snippet=" ".join(r.get("highlights") or []), source="exa")
            for r in data.get("results", [])
        ]  # fmt: skip

    async def search(self, query, *, limit=5, sort="relevance"):
        engines = [self._ddg, self._brave]
        if os.environ.get("WIRE_SEARXNG_URL"):
            engines.append(self._searxng)
        if os.environ.get("EXA_API_KEY"):
            engines.append(self._exa)

        async def safe(engine):
            try:
                return await asyncio.wait_for(engine(query), ENGINE_TIMEOUT_S)
            except (TimeoutError, RequestException, WireFetchError, ValueError, KeyError):
                return []  # one engine down must not sink the search

        return fuse(await asyncio.gather(*(safe(e) for e in engines)), limit)


CHANNEL = SearchChannel
