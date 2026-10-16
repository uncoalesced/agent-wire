"""Reddit: .json threads/listings with old.reddit mirror and PullPush failover."""

import base64
import os
import time
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireComment, WireSearchHit

WWW = "https://www.reddit.com"
PULLPUSH = "https://api.pullpush.io/reddit/search"
PRUNE_DEPTH, PRUNE_SCORE = 3, 2


def parse_comments(children: list, depth: int = 0) -> list[WireComment]:
    out = []
    for ch in children:
        if ch.get("kind") != "t1":
            continue  # "more" stubs
        d = ch["data"]
        score = d.get("score") or 0
        if depth > PRUNE_DEPTH and score < PRUNE_SCORE:
            continue
        replies = d.get("replies")
        kids = replies["data"]["children"] if isinstance(replies, dict) else []
        out.append(
            WireComment(
                id=d["id"], body=d.get("body") or "", author_handle=d.get("author"),
                score=score, depth=depth, is_op=bool(d.get("is_submitter")),
                timestamp=ts(d.get("created_utc")), replies=parse_comments(kids, depth + 1),
            )
        )  # fmt: skip
    return out


def parse_thread(data: list) -> dict:
    post = data[0]["data"]["children"][0]["data"]
    body = post.get("selftext") or ""
    if not post.get("is_self") and post.get("url"):
        body = f"Link: {post['url']}\n\n{body}".strip()
    return {
        "title": post.get("title"),
        "author_handle": post.get("author"),
        "timestamp": ts(post.get("created_utc")),
        "score": post.get("score"),
        "body": body,
        "comments": parse_comments(data[1]["data"]["children"]) if len(data) > 1 else [],
        "metadata": {
            "subreddit": post.get("subreddit_name_prefixed"),
            "comments": post.get("num_comments"),
            "upvote_ratio": post.get("upvote_ratio"),
        },
    }


def parse_pullpush(post: dict, comments: list[dict]) -> dict:
    by_parent: dict[str, list[dict]] = {}
    for c in comments:
        by_parent.setdefault(c.get("parent_id", ""), []).append(c)

    def build(parent: str, depth: int) -> list[WireComment]:
        return [
            WireComment(id=c["id"], body=c.get("body") or "", author_handle=c.get("author"),
                        score=c.get("score") or 0, depth=depth, timestamp=ts(c.get("created_utc")),
                        is_op=c.get("author") == post.get("author"),
                        replies=build(f"t1_{c['id']}", depth + 1))
            for c in by_parent.get(parent, [])
        ]  # fmt: skip

    return {
        "title": post.get("title"), "author_handle": post.get("author"),
        "timestamp": ts(post.get("created_utc")), "score": post.get("score"),
        "body": post.get("selftext") or post.get("url") or "",
        "comments": build(f"t3_{post['id']}", 0),
        "metadata": {"subreddit": f"r/{post.get('subreddit')}", "source": "pullpush archive"},
    }  # fmt: skip


def listing_items(data: dict) -> list[dict]:
    items = []
    for ch in data.get("data", {}).get("children", []):
        d = ch.get("data", {})
        if ch.get("kind") == "t1":  # a comment in a user listing
            items.append({"title": f"comment in {d.get('link_title', '')}",
                          "url": WWW + d.get("permalink", ""), "meta": [f"+{d.get('score', 0)}"],
                          "snippet": d.get("body", "")})  # fmt: skip
            continue
        items.append({
            "title": d.get("title"), "url": WWW + d.get("permalink", ""),
            "meta": [f"+{d.get('score', 0)}", f"{d.get('num_comments', 0)} comments",
                     d.get("subreddit_name_prefixed"), f"u/{d.get('author')}"],
            "snippet": d.get("selftext", ""),
        })  # fmt: skip
    return items


def _path(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if (parts.hostname or "").endswith("redd.it"):
        return f"/comments{path}"
    return path.removesuffix(".json")


class RedditChannel(Channel):
    name = "reddit"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._token: str | None = None
        self._token_expires = 0.0

    async def _app_token(self) -> str | None:
        """Reddit's official app-only OAuth (no user login) when REDDIT_CLIENT_ID is set.

        Reddit blocks anonymous .json from many networks; an app token restores live reads.
        """
        client_id = os.environ.get("REDDIT_CLIENT_ID")
        if not client_id:
            return None
        if self._token and time.time() < self._token_expires:
            return self._token
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        grant = (
            "client_credentials" if secret else "https://oauth.reddit.com/grants/installed_client"
        )
        f = await self.ctx.pool.get(
            "https://www.reddit.com/api/v1/access_token", method="POST", impersonate=None,
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": grant, "device_id": "DO_NOT_TRACK_THIS_DEVICE"},
        )  # fmt: skip
        body = parse_json(f.content) if f.ok else {}
        self._token = body.get("access_token")
        self._token_expires = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    async def _json(self, path: str, query: str = "", **kw):
        sep = "&" if query else ""
        if kw.get("tool") != "wire_me" and (token := await self._app_token()):
            try:
                return await self.fetch_json(
                    f"https://oauth.reddit.com{path}?raw_json=1{sep}{query}",
                    headers={"Authorization": f"Bearer {token}"}, **kw,
                )  # fmt: skip
            except (WireFetchError, ValueError):
                pass  # fall through to the public endpoints
        url = f"{WWW}{path}.json?raw_json=1{sep}{query}"
        mirror = f"https://old.reddit.com{path}.json?raw_json=1{sep}{query}"
        return await self.fetch_json(url, mirrors=[mirror], **kw)

    async def read(self, url, *, query=None, section=None, include_comments=True):
        path = _path(url)
        segs = [s for s in path.split("/") if s]
        if "s" in segs[2:3]:  # /r/sub/s/<code> share link: let Reddit resolve it
            f = await self.fetch(url, archive=False)
            path, segs = _path(f.url), [s for s in _path(f.url).split("/") if s]
        if "comments" in segs:
            post_id = segs[segs.index("comments") + 1]
            try:
                data, f = await self._json(path, "limit=200&sort=top")
                return self.doc(f, url, **parse_thread(data))
            except WireFetchError:
                return await self._pullpush(url, post_id)
        if segs[:1] in (["r"], ["user"], ["u"]):
            return await self.feed("/".join(segs), limit=25)
        return await self.ctx.channel("web").read(url, query=query, section=section)

    async def _pullpush(self, url, post_id):
        posts, f = await self.fetch_json(f"{PULLPUSH}/submission/?ids={post_id}")
        if not posts.get("data"):
            raise WireFetchError(f"{url}: reddit blocked and not in the PullPush archive")
        comments, _ = await self.fetch_json(f"{PULLPUSH}/comment/?link_id={post_id}&size=100")
        return self.doc(f, url, **parse_pullpush(posts["data"][0], comments.get("data", [])))

    async def search(self, query, *, limit=5, sort="relevance"):
        data, _ = await self._json("/search", f"q={quote(query)}&sort={sort}&limit={limit}")
        return [
            WireSearchHit(url=it["url"], title=it["title"] or "", snippet=it["snippet"][:300],
                          source="reddit", rank_score=float(str(it["meta"][0]).lstrip("+") or 0))
            for it in listing_items(data)[:limit]
        ]  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().strip("/").removeprefix("https://www.reddit.com/")
        if t.startswith(("u/", "user/", "@")):
            user = t.split("/", 1)[-1].lstrip("@")
            path, title = f"/user/{user}/submitted", f"u/{user}"
        else:
            sub = t.removeprefix("r/").split("/")[0]
            path, title = f"/r/{sub}/{sort}", f"r/{sub} · {sort}"
        data, f = await self._json(path, f"limit={limit}")
        return self.doc(f, f"{WWW}{path}", title=title, body=listing(listing_items(data)[:limit]))

    async def me(self, target="latest_posts", *, limit=5):
        paths = {"latest_posts": "submitted", "likes": "upvoted", "bookmarks": "saved",
                 "comments": "comments", "stats": "about"}  # fmt: skip
        if target not in paths:
            raise WireUnsupported(
                "reddit targets: " + ", ".join(paths) + " (notifications live in the private inbox,"
                " which agent-wire never reads)"
            )
        me, _ = await self.fetch_json(f"{WWW}/api/me.json", tool="wire_me")
        name = (me.get("data") or me).get("name")
        if not name:
            raise WireUnsupported(
                "not logged in to reddit locally; run `wire auth login reddit.com`"
            )
        data, f = await self.fetch_json(f"{WWW}/user/{name}/{paths[target]}.json?limit={limit}&raw_json=1",
                                        tool="wire_me")  # fmt: skip
        if target == "stats":
            d = data.get("data", {})
            body = f"u/{name}: {d.get('link_karma')} post karma · {d.get('comment_karma')} comment karma"
        else:
            body = listing(listing_items(data)[:limit])
        return self.doc(f, f"{WWW}/user/{name}", title=f"my reddit {target}", body=body)


CHANNEL = RedditChannel
