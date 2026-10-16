"""Substack (posts API + comments) and Medium (reader with Wayback fallback on member-only stories)."""

from urllib.parse import urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, ts
from agent_wire.channels.tier1_global.web import extract_article
from agent_wire.engine.waterfall import WireFetchError, wayback_snapshot
from agent_wire.models import WireComment

_PAYWALL = ("member-only story", "this post is for paid subscribers", "upgrade to paid")


def parse_substack_comments(items: list[dict], depth: int = 0) -> list[WireComment]:
    return [
        WireComment(id=str(c.get("id")), body=c.get("body") or "", author_handle=c.get("name"),
                    score=c.get("reaction_count") or sum((c.get("reactions") or {}).values()),
                    timestamp=ts(c.get("date")), depth=depth,
                    replies=parse_substack_comments(c.get("children") or [], depth + 1))
        for c in items if c.get("body")
    ]  # fmt: skip


class PublishingChannel(Channel):
    name = "publishing"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        p = urlsplit(url)
        host = p.hostname or ""
        segs = [s for s in p.path.split("/") if s]
        if segs[:1] == ["p"] and len(segs) > 1 and "medium.com" not in host:
            return await self._substack(url, host, segs[1], include_comments)
        return await self._medium(url, query, section)

    async def _substack(self, url, host, slug, include_comments):
        post, f = await self.fetch_json(f"https://{host}/api/v1/posts/{slug}")
        body = html_to_markdown(post.get("body_html") or "")
        if post.get("audience") == "only_paid" and len(body) < 2000:
            body += "\n\n_Paid post: only the free preview is public._"
        comments = []
        if include_comments and post.get("id") and post.get("comment_count"):
            try:
                data, _ = await self.fetch_json(
                    f"https://{host}/api/v1/post/{post['id']}/comments?all_comments=true&sort=best_first"
                )
                comments = parse_substack_comments(data.get("comments", []))
            except (WireFetchError, ValueError):
                comments = []
        bylines = post.get("publishedBylines") or [{}]
        return self.doc(
            f, url, title=post.get("title"), author_handle=bylines[0].get("name"),
            timestamp=ts(post.get("post_date")), body=f"{post.get('subtitle') or ''}\n\n{body}".strip(),
            comments=comments, score=post.get("reaction_count"),
            metadata={"comments": post.get("comment_count"), "audience": post.get("audience")},
        )  # fmt: skip

    async def _medium(self, url, query, section):
        doc = await self.ctx.channel("web").read(url, query=query, section=section)
        gated = any(m in doc.body.lower()[:4000] for m in _PAYWALL) or len(doc.body) < 1200
        if gated and (snap := await wayback_snapshot(self.ctx.pool, url)):
            f = await self.fetch(snap, archive=False, browser=False)
            body, info = extract_article(f.text)
            if len(body) > len(doc.body):
                return self.doc(f, url, title=info["title"] or doc.title, author_handle=info["author"],
                                body=body, metadata={"archived": snap})  # fmt: skip
        return doc.model_copy(update={"channel": self.name})

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().rstrip("/")
        if t.startswith("@") or "medium.com" in t:
            handle = t.split("medium.com/")[-1]
            return await self.ctx.channel("rss").feed(
                f"https://medium.com/feed/{handle}", limit=limit
            )
        host = (
            urlsplit(t).hostname if "//" in t else f"{t.removesuffix('.substack.com')}.substack.com"
        )
        if not host:
            raise WireUnsupported(
                "publishing feeds: '@medium_user', 'name' (Substack) or a newsletter URL"
            )
        return await self.ctx.channel("rss").feed(f"https://{host}/feed", limit=limit)


CHANNEL = PublishingChannel
