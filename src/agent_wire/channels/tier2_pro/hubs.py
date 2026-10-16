"""Package and model hubs: PyPI, npm, crates.io, Hugging Face (models + datasets)."""

import asyncio
from urllib.parse import quote, urlsplit

from curl_cffi.requests.exceptions import RequestException
from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.models import WireSearchHit

SEARCH_ERRORS = (WireFetchError, RequestException, ValueError, KeyError)


def parse_pypi_search(html: str) -> list[WireSearchHit]:
    hits = []
    for a in LexborHTMLParser(html).css("a.package-snippet"):
        name = a.css_first(".package-snippet__name")
        desc = a.css_first(".package-snippet__description")
        if name:
            hits.append(WireSearchHit(url=f"https://pypi.org{a.attributes.get('href', '')}",
                                      title=f"pypi: {name.text(strip=True)}",
                                      snippet=desc.text(strip=True) if desc else "", source="pypi"))  # fmt: skip
    return hits


class HubsChannel(Channel):
    name = "hubs"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        p = urlsplit(url)
        host, segs = (p.hostname or "").removeprefix("www."), [s for s in p.path.split("/") if s]
        if host == "pypi.org" and segs[:1] == ["project"] and len(segs) > 1:
            return await self._pypi(url, segs[1], segs[2] if len(segs) > 2 else None)
        if host.endswith("npmjs.com") and segs[:1] == ["package"] and len(segs) > 1:
            name = "/".join(segs[1:3]) if segs[1].startswith("@") else segs[1]
            return await self._npm(url, name)
        if host == "crates.io" and segs[:1] == ["crates"] and len(segs) > 1:
            return await self._crate(url, segs[1])
        if host == "huggingface.co" and segs:
            kind = (
                "datasets"
                if segs[0] == "datasets"
                else "spaces"
                if segs[0] == "spaces"
                else "models"
            )
            repo = "/".join(segs[1:3] if kind != "models" else segs[:2])
            return await self._hf(url, kind, repo)
        return await self.ctx.channel("web").read(url, query=query, section=section)

    async def _pypi(self, url, name, version):
        path = f"{name}/{version}" if version else name
        data, f = await self.fetch_json(f"https://pypi.org/pypi/{path}/json")
        info = data.get("info", {})
        urls = info.get("project_urls") or {}
        meta = {"version": info.get("version"), "license": (info.get("license") or "")[:60] or None,
                "requires_python": info.get("requires_python"),
                "homepage": urls.get("Homepage") or urls.get("Source") or info.get("home_page")}  # fmt: skip
        body = f"{info.get('summary') or ''}\n\n{info.get('description') or ''}".strip()
        return self.doc(f, url, title=f"{info.get('name', name)} (PyPI)", author_handle=info.get("author"),
                        body=body, metadata=meta)  # fmt: skip

    async def _npm(self, url, name):
        (data, f), downloads = await asyncio.gather(
            self.fetch_json(f"https://registry.npmjs.org/{name.replace('/', '%2F')}"),
            self._safe_json(f"https://api.npmjs.org/downloads/point/last-week/{name}"),
        )
        latest = (data.get("dist-tags") or {}).get("latest")
        ver = (data.get("versions") or {}).get(latest, {})
        repo = data.get("repository") or {}
        meta = {"version": latest, "license": data.get("license") if isinstance(data.get("license"), str) else None,
                "weekly_downloads": downloads.get("downloads"),
                "repository": repo.get("url") if isinstance(repo, dict) else repo,
                "modified": (data.get("time") or {}).get("modified", "")[:10] or None}  # fmt: skip
        body = f"{data.get('description') or ver.get('description') or ''}\n\n{data.get('readme') or ''}".strip()
        return self.doc(f, url, title=f"{name} (npm)", body=body, metadata=meta)

    async def _crate(self, url, name):
        data, f = await self.fetch_json(f"https://crates.io/api/v1/crates/{name}")
        c = data.get("crate", {})
        readme = ""
        if ver := c.get("max_stable_version") or c.get("max_version"):
            try:
                rf = await self.fetch(f"https://crates.io/api/v1/crates/{name}/{ver}/readme",
                                      archive=False, browser=False)  # fmt: skip
                readme = html_to_markdown(rf.text)
            except WireFetchError:
                readme = ""
        meta = {"version": ver, "downloads": c.get("downloads"), "recent_downloads": c.get("recent_downloads"),
                "repository": c.get("repository")}  # fmt: skip
        return self.doc(f, url, title=f"{name} (crates.io)", body=f"{c.get('description') or ''}\n\n{readme}".strip(),
                        metadata=meta)  # fmt: skip

    async def _hf(self, url, kind, repo):
        api = f"https://huggingface.co/api/{kind}/{repo}"
        raw = f"https://huggingface.co/{'' if kind == 'models' else kind + '/'}{repo}/raw/main/README.md"
        (data, f), readme = await asyncio.gather(self.fetch_json(api), self._safe_text(raw))
        meta = {"downloads": data.get("downloads"), "likes": data.get("likes"),
                "pipeline": data.get("pipeline_tag"), "library": data.get("library_name"),
                "license": next((t.split(":", 1)[1] for t in data.get("tags", []) if t.startswith("license:")), None),
                "updated": (data.get("lastModified") or "")[:10] or None}  # fmt: skip
        return self.doc(f, url, title=f"{repo} (Hugging Face {kind[:-1]})", author_handle=data.get("author"),
                        body=readme, metadata=meta)  # fmt: skip

    async def _safe_json(self, url) -> dict:
        try:
            return (await self.fetch_json(url))[0]
        except SEARCH_ERRORS:
            return {}

    async def _safe_text(self, url) -> str:
        try:
            return (await self.fetch(url, archive=False, browser=False)).text
        except SEARCH_ERRORS:
            return ""

    async def search(self, query, *, limit=5, sort="relevance"):
        q = quote(query)

        async def pypi():
            f = await self.fetch(f"https://pypi.org/search/?q={q}", archive=False, browser=False)
            return parse_pypi_search(f.text)

        async def npm():
            d, _ = await self.fetch_json(
                f"https://registry.npmjs.org/-/v1/search?text={q}&size={limit}"
            )
            return [WireSearchHit(url=o["package"]["links"]["npm"], title=f"npm: {o['package']['name']}",
                                  snippet=o["package"].get("description") or "", source="npm")
                    for o in d.get("objects", [])]  # fmt: skip

        async def crates():
            d, _ = await self.fetch_json(f"https://crates.io/api/v1/crates?q={q}&per_page={limit}")
            return [WireSearchHit(url=f"https://crates.io/crates/{c['name']}", title=f"crate: {c['name']}",
                                  snippet=c.get("description") or "", source="crates")
                    for c in d.get("crates", [])]  # fmt: skip

        async def hf():
            d, _ = await self.fetch_json(
                f"https://huggingface.co/api/models?search={q}&limit={limit}&sort=downloads"
            )
            return [WireSearchHit(url=f"https://huggingface.co/{m['id']}", title=f"hf: {m['id']}",
                                  snippet=f"{m.get('downloads', 0)} downloads · {m.get('pipeline_tag') or ''}",
                                  source="huggingface") for m in d]  # fmt: skip

        async def safe(fn):
            try:
                return (await fn())[:limit]
            except SEARCH_ERRORS:
                return []

        lists = await asyncio.gather(*(safe(fn) for fn in (pypi, npm, crates, hf)))
        hits = [h for group in lists for h in group]
        if not hits:
            raise WireUnsupported("no package/model hub returned results")
        return hits


CHANNEL = HubsChannel
