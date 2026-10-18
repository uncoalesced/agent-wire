"""Bluesky (public AT Protocol AppView) and Mastodon (ActivityPub REST), zero auth."""

from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, listing, ts
from agent_wire.models import WireComment, WireSearchHit

BSKY = "https://api.bsky.app/xrpc"  # public.api.bsky.app 403s searchPosts for anonymous callers


def _bsky_url(post: dict) -> str:
    rkey = post.get("uri", "").rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{post.get('author', {}).get('handle')}/post/{rkey}"


def _bsky_hit(p: dict) -> WireSearchHit:
    handle = p.get("author", {}).get("handle")
    return WireSearchHit(
        url=_bsky_url(p), title=f"@{handle}", snippet=p.get("record", {}).get("text", ""),
        source="bluesky", rank_score=float(p.get("likeCount") or 0),
        timestamp=ts(p.get("record", {}).get("createdAt")), author=handle,
        engagement={"likes": p.get("likeCount") or 0, "reposts": p.get("repostCount") or 0,
                    "comments": p.get("replyCount") or 0},
    )  # fmt: skip


def parse_bsky_thread(
    thread: dict, op: str | None = None, depth: int = 0
) -> tuple[dict, list[WireComment]]:
    post = thread.get("post", {})
    author = post.get("author", {}).get("handle")
    replies = [
        WireComment(
            id=r["post"]["cid"], body=r["post"].get("record", {}).get("text", ""),
            author_handle=r["post"].get("author", {}).get("handle"), score=r["post"].get("likeCount") or 0,
            timestamp=ts(r["post"].get("record", {}).get("createdAt")), depth=depth,
            is_op=r["post"].get("author", {}).get("handle") == (op or author),
            replies=parse_bsky_thread(r, op or author, depth + 1)[1],
        )
        for r in thread.get("replies") or [] if r.get("post")
    ]  # fmt: skip
    return post, replies


def parse_mastodon_context(descendants: list[dict], root_id: str, op: str) -> list[WireComment]:
    children: dict[str, list[dict]] = {}
    for s in descendants:
        children.setdefault(s.get("in_reply_to_id"), []).append(s)

    def build(parent: str, depth: int) -> list[WireComment]:
        return [WireComment(id=s["id"], body=html_to_markdown(s.get("content", "")),
                            author_handle=s.get("account", {}).get("acct"), score=s.get("favourites_count") or 0,
                            timestamp=ts(s.get("created_at")), depth=depth,
                            is_op=s.get("account", {}).get("acct") == op, replies=build(s["id"], depth + 1))
                for s in children.get(parent, [])]  # fmt: skip

    return build(root_id, 0)


class FediverseChannel(Channel):
    name = "fediverse"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        p = urlsplit(url)
        segs = [s for s in p.path.split("/") if s]
        if (p.hostname or "").endswith("bsky.app"):
            if len(segs) >= 4 and segs[0] == "profile" and segs[2] == "post":
                return await self._bsky_post(url, segs[1], segs[3])
            if len(segs) >= 2 and segs[0] == "profile":
                return await self.feed(f"bsky:{segs[1]}")
            raise WireUnsupported("bluesky: expected /profile/<handle>[/post/<id>]")
        if segs and segs[0].startswith("@") and len(segs) > 1 and segs[1].isdigit():
            return await self._mastodon(url, f"https://{p.hostname}", segs[1], include_comments)
        return await self.ctx.channel("web").read(url, query=query, section=section)

    async def _did(self, actor: str) -> str:
        if actor.startswith("did:"):
            return actor
        data, _ = await self.fetch_json(f"{BSKY}/com.atproto.identity.resolveHandle?handle={actor}")
        return data["did"]

    async def _bsky_post(self, url, actor, rkey):
        did = await self._did(actor)
        uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        data, f = await self.fetch_json(
            f"{BSKY}/app.bsky.feed.getPostThread?uri={quote(uri)}&depth=6"
        )
        post, replies = parse_bsky_thread(data.get("thread", {}))
        record = post.get("record", {})
        embed = (post.get("embed") or {}).get("external") or {}
        body = record.get("text", "") + (
            f"\n\nLink: {embed['uri']} ({embed.get('title', '')})" if embed.get("uri") else ""
        )
        return self.doc(
            f, url, title=f"@{post.get('author', {}).get('handle')} on Bluesky",
            author_handle=post.get("author", {}).get("handle"), timestamp=ts(record.get("createdAt")),
            score=post.get("likeCount"), body=body, comments=replies,
            metadata={"likes": post.get("likeCount"), "reposts": post.get("repostCount"),
                      "replies": post.get("replyCount")},
        )  # fmt: skip

    async def _mastodon(self, url, base, status_id, include_comments):
        status, f = await self.fetch_json(f"{base}/api/v1/statuses/{status_id}")
        op = status.get("account", {}).get("acct")
        comments = []
        if include_comments:
            ctx, _ = await self.fetch_json(f"{base}/api/v1/statuses/{status_id}/context")
            comments = parse_mastodon_context(ctx.get("descendants", []), status_id, op)
        return self.doc(
            f, url, title=f"@{op} on {urlsplit(base).hostname}", author_handle=op,
            timestamp=ts(status.get("created_at")), score=status.get("favourites_count"),
            body=html_to_markdown(status.get("content", "")), comments=comments,
            metadata={"boosts": status.get("reblogs_count"), "replies": status.get("replies_count")},
        )  # fmt: skip

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        order = "latest" if sort in ("new", "latest") else "top"
        window = f"&since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}" if since else ""
        data, _ = await self.fetch_json(
            f"{BSKY}/app.bsky.feed.searchPosts?q={quote(query)}&limit={limit}&sort={order}{window}"
        )
        return [_bsky_hit(p) for p in data.get("posts", [])]

    async def mastodon_tag(self, tag: str, *, limit=10, instance="mastodon.social"):
        """Public hashtag timeline (no auth on most instances)."""
        tag = "".join(ch for ch in tag if ch.isalnum() or ch == "_")
        data, _ = await self.fetch_json(
            f"https://{instance}/api/v1/timelines/tag/{tag}?limit={limit}"
        )
        hits = []
        for s in data:
            s = s.get("reblog") or s
            acct = (s.get("account") or {}).get("acct")
            hits.append(WireSearchHit(
                url=s.get("url") or s.get("uri", ""), title=f"@{acct}",
                snippet=html_to_markdown(s.get("content", ""))[:300], source="mastodon",
                rank_score=float(s.get("favourites_count") or 0), timestamp=ts(s.get("created_at")),
                author=acct, engagement={"likes": s.get("favourites_count") or 0,
                                         "reposts": s.get("reblogs_count") or 0,
                                         "comments": s.get("replies_count") or 0},
            ))  # fmt: skip
        return hits

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().removeprefix("bsky:")
        if t.count("@") == 2 or (t.startswith("@") and "@" in t[1:]):  # @user@instance -> Mastodon
            user, instance = t.lstrip("@").split("@", 1)
            base = f"https://{instance}"
            acct, f = await self.fetch_json(f"{base}/api/v1/accounts/lookup?acct={user}")
            statuses, f = await self.fetch_json(
                f"{base}/api/v1/accounts/{acct['id']}/statuses?limit={limit}&exclude_replies=true"
            )
            statuses = [s.get("reblog") or s for s in statuses]  # boosts carry their content inside
            items = [{"title": html_to_markdown(s.get("content", ""))[:120] or s.get("url"), "url": s.get("url"),
                      "meta": [f"♥{s.get('favourites_count', 0)}", f"↻{s.get('reblogs_count', 0)}",
                               (s.get("created_at") or "")[:10]]} for s in statuses]  # fmt: skip
            return self.doc(f, f"{base}/@{user}", title=f"@{user}@{instance}", body=listing(items))
        actor = t.lstrip("@")
        data, f = await self.fetch_json(
            f"{BSKY}/app.bsky.feed.getAuthorFeed?actor={actor}&limit={limit}&filter=posts_no_replies"
        )
        posts = [i["post"] for i in data.get("feed", [])]
        items = [{"title": p.get("record", {}).get("text", "")[:120], "url": _bsky_url(p),
                  "meta": [f"♥{p.get('likeCount', 0)}", f"↻{p.get('repostCount', 0)}",
                           (p.get("record", {}).get("createdAt") or "")[:10]]} for p in posts]  # fmt: skip
        return self.doc(
            f,
            f"https://bsky.app/profile/{actor}",
            title=f"@{actor} on Bluesky",
            body=listing(items),
        )


CHANNEL = FediverseChannel
