"""DEV Community (dev.to public API, no key): developer write-ups by tag, with reactions."""

import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, listing, ts
from agent_wire.models import WireComment, WireSearchHit

API = "https://dev.to/api"


def tag_of(text: str) -> str:
    """dev.to tags are single lower-case alphanumeric words; use the longest topic word."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    if not words:
        raise WireUnsupported("devto: give a topic word, e.g. 'rust' or 'webdev'")
    return max(words, key=len)


def article_hit(a: dict) -> WireSearchHit:
    return WireSearchHit(
        url=a.get("url") or "", title=a.get("title") or "", snippet=a.get("description") or "",
        source="devto", rank_score=float(a.get("public_reactions_count") or 0),
        timestamp=ts(a.get("published_at")), author=(a.get("user") or {}).get("username"),
        engagement={"likes": a.get("public_reactions_count") or 0,
                    "comments": a.get("comments_count") or 0},
    )  # fmt: skip


def _tags(a: dict) -> str | None:
    """The list endpoint sends tag_list as a list, the article endpoint as 'a, b' (and tags as a list)."""
    raw = a.get("tag_list") or a.get("tags")
    return ", ".join(raw) if isinstance(raw, list) else raw or None


def _comments(items: list, depth: int = 0) -> list[WireComment]:
    return [WireComment(id=c.get("id_code") or str(i), body=html_to_markdown(c.get("body_html") or ""),
                        author_handle=(c.get("user") or {}).get("username"),
                        timestamp=ts(c.get("created_at")), depth=depth,
                        replies=_comments(c.get("children") or [], depth + 1))
            for i, c in enumerate(items)]  # fmt: skip


class DevToChannel(Channel):
    name = "devto"

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        days = 30 if since is None else max(1, (datetime.now(UTC) - since).days)
        data, _ = await self.fetch_json(
            f"{API}/articles?tag={quote(tag_of(query))}&top={days}&per_page={limit}"
        )
        return [article_hit(a) for a in data[:limit]]

    async def feed(self, target, *, sort="hot", limit=10):
        tag = tag_of(target)
        data, f = await self.fetch_json(f"{API}/articles?tag={quote(tag)}&top=7&per_page={limit}")
        items = [{"title": h.title, "url": h.url, "snippet": h.snippet,
                  "meta": [f"♥{h.engagement['likes']}", f"{h.engagement['comments']} comments", h.author]}
                 for h in map(article_hit, data)]  # fmt: skip
        return self.doc(
            f, f"https://dev.to/t/{tag}/top/week", title=f"dev.to · #{tag}", body=listing(items)
        )

    async def read(self, url, *, query=None, section=None, include_comments=True):
        parts = [p for p in urlsplit(url).path.split("/") if p]
        if len(parts) != 2:
            return await self.ctx.channel("web").read(url, query=query, section=section)
        a, f = await self.fetch_json(f"{API}/articles/{parts[0]}/{parts[1]}")
        comments = []
        if include_comments and a.get("id"):
            raw, _ = await self.fetch_json(f"{API}/comments?a_id={a['id']}")
            comments = _comments(raw)
        return self.doc(
            f, url, title=a.get("title"), author_handle=(a.get("user") or {}).get("username"),
            timestamp=ts(a.get("published_at")), score=a.get("public_reactions_count"),
            body=a.get("body_markdown") or html_to_markdown(a.get("body_html") or ""),
            comments=comments, metadata={"tags": _tags(a), "comments": a.get("comments_count")},
        )  # fmt: skip


CHANNEL = DevToChannel
