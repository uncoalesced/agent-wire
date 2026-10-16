"""Wayback Machine and Archive.today: read snapshots, list a URL's capture history."""

import re
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, listing
from agent_wire.channels.tier1_global.web import extract_article
from agent_wire.engine.waterfall import wayback_snapshot
from agent_wire.models import WireSearchHit

_WAYBACK = re.compile(r"^/web/(\d{1,14})(?:[a-z_]{2,3}_)?/(.+)$")


def raw_wayback(url: str) -> str:
    """Wayback page URL -> the `id_` form, which serves the original bytes without the toolbar."""
    m = _WAYBACK.match(
        urlsplit(url).path + (f"?{urlsplit(url).query}" if urlsplit(url).query else "")
    )
    return f"https://web.archive.org/web/{m[1]}id_/{m[2]}" if m else url


def parse_cdx(rows: list) -> list[dict]:
    if not rows:
        return []
    header, *data = rows
    return [dict(zip(header, r, strict=False)) for r in data]


class ArchiveChannel(Channel):
    name = "archive"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        host = urlsplit(url).hostname or ""
        if host == "web.archive.org":
            target = raw_wayback(url)
        elif host in ("archive.ph", "archive.today", "archive.is"):
            target = url
        else:  # any other URL: read its newest Wayback snapshot
            target = await wayback_snapshot(self.ctx.pool, url)
            if not target:
                raise WireUnsupported(f"no Wayback snapshot of {url}")
        f = await self.fetch(target, archive=False)
        body, info = extract_article(f.text)
        return self.doc(f, url, title=info["title"], author_handle=info["author"], body=body,
                        metadata={"snapshot": target})  # fmt: skip

    async def search(self, query, *, limit=5, sort="relevance"):
        """Capture history of a URL (newest first)."""
        if "." not in query:
            raise WireUnsupported("archive search takes a URL and lists its Wayback captures")
        data, _ = await self.fetch_json(
            f"https://web.archive.org/cdx/search/cdx?url={quote(query)}&output=json&limit=-{limit}"
            "&fl=timestamp,original,statuscode&filter=statuscode:200"
        )
        caps = list(reversed(parse_cdx(data)))
        return [WireSearchHit(url=f"https://web.archive.org/web/{c['timestamp']}/{c['original']}",
                              title=f"{c['original']} @ {c['timestamp'][:8]}", snippet=f"HTTP {c.get('statuscode')}",
                              source="wayback") for c in caps[:limit]]  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        hits = await self.search(target, limit=limit)
        return self.doc(None, target, title=f"Wayback captures of {target}",
                        body=listing([{"title": h.title, "url": h.url} for h in hits]))  # fmt: skip


CHANNEL = ArchiveChannel
