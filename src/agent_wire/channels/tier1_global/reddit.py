"""Reddit: .json threads/listings (official app OAuth when configured, old.reddit mirror).

When anonymous .json is blocked, search tries Reddit's own search RSS (live, no scores), then the
PullPush and Arctic Shift archives (free, volunteer APIs: small requests only, copies are captured
shortly after posting so scores lag the live site). Thread reads fall back to Arctic Shift.
"""

import asyncio
import base64
import os
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote, urlencode, urlsplit

import defusedxml.ElementTree as ET  # untrusted feeds: no entity expansion, no DTDs

from agent_wire.channels.base import Channel, WireUnsupported, html_text, listing, ts
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireComment, WireSearchHit

WWW = "https://www.reddit.com"
ARCTIC = "https://arctic-shift.photon-reddit.com/api"
PULLPUSH = "https://api.pullpush.io/reddit/search/submission/"
ATOM = "{http://www.w3.org/2005/Atom}"
ARCTIC_NOTE = "Arctic Shift archive copy (scores/comments as captured, not live)"
_STOP = frozenset({"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "vs",
                   "versus", "what", "is", "are", "how", "why", "best", "new"})  # fmt: skip
PRUNE_DEPTH, PRUNE_SCORE = 3, 2
BLOCKED_HINT = (
    "Reddit blocks anonymous access from this network. Set REDDIT_CLIENT_ID (a free 'installed app' "
    "at reddit.com/prefs/apps) or run `wire auth login reddit.com`."
)


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
        link = WWW + post["url"] if post["url"].startswith("/") else post["url"]  # crossposts
        body = f"Link: {link}\n\n{body}".strip()
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


def post_hit(d: dict, archived: bool = False) -> WireSearchHit:
    link = None if d.get("is_self") or "reddit.com" in (d.get("url") or "") else d.get("url")
    return WireSearchHit(
        url=WWW + d.get("permalink", ""), title=d.get("title") or "",
        snippet="" if d.get("selftext") in ("[removed]", "[deleted]") else (d.get("selftext") or "")[:300],
        source="reddit",
        rank_score=float(d.get("score") or 0), timestamp=ts(d.get("created_utc")),
        author=d.get("author"), link=link,
        engagement={"score": d.get("score") or 0, "comments": d.get("num_comments") or 0,
                    **({"archived": 1} if archived else {})},
    )  # fmt: skip


def rss_hits(content: bytes) -> list[WireSearchHit]:
    """Reddit search .rss (Atom): posts only (t3_), no score or comment counts in the feed."""
    hits = []
    for e in ET.fromstring(content).iter(f"{ATOM}entry"):
        if not (e.findtext(f"{ATOM}id") or "").startswith("t3_"):
            continue  # t5_ = subreddit results
        link = e.find(f"{ATOM}link")
        body = e.findtext(f"{ATOM}content") or ""
        out = re.search(r'href="([^"]+)">\[link\]', body)
        author = (e.findtext(f"{ATOM}author/{ATOM}name") or "").removeprefix("/u/")
        hits.append(WireSearchHit(
            url=link.get("href", "") if link is not None else "", title=e.findtext(f"{ATOM}title") or "",
            snippet=html_text(body).split("submitted by")[0].strip()[:300], source="reddit",
            timestamp=ts(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")),
            author=author or None,
            link=out.group(1) if out and "reddit.com" not in out.group(1) else None,
        ))  # fmt: skip
    return hits


def rss_window(since: datetime | None) -> str:
    if since is None:
        return "all"
    days = (datetime.now(UTC) - since).days
    return next((t for n, t in ((1, "day"), (7, "week"), (31, "month"), (365, "year")) if days <= n),
                "all")  # fmt: skip


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
        try:
            return await self.fetch_json(url, mirrors=[mirror], **kw)
        except WireFetchError as e:
            raise WireFetchError(f"{e} {BLOCKED_HINT}") from e
        except ValueError as e:  # an HTML block page where JSON should be
            raise WireFetchError(f"{url}: {BLOCKED_HINT}") from e

    async def read(self, url, *, query=None, section=None, include_comments=True):
        path = _path(url)
        segs = [s for s in path.split("/") if s]
        if "s" in segs[2:3]:  # /r/sub/s/<code> share link: let Reddit resolve it
            f = await self.fetch(url, archive=False)
            path, segs = _path(f.url), [s for s in _path(f.url).split("/") if s]
        if "comments" in segs:
            try:
                data, f = await self._json(path, "limit=200&sort=top")
            except WireFetchError:
                return await self._arctic_thread(url, segs[segs.index("comments") + 1])
            return self.doc(f, url, **parse_thread(data))
        if segs[:1] in (["r"], ["user"], ["u"]):
            return await self.feed("/".join(segs), limit=25)
        return await self.ctx.channel("web").read(url, query=query, section=section)

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        window = "&t=month" if since else ""
        try:
            data, _ = await self._json(
                "/search", f"q={quote(query)}&sort={sort}&limit={limit}{window}"
            )
        except WireFetchError as blocked:
            for fallback in (self._rss_search, self._pullpush_search, self._arctic_search):
                try:
                    if hits := await fallback(query, limit, since, sort):
                        return hits
                except (WireFetchError, ValueError, ET.ParseError):
                    continue
            raise WireFetchError(
                f"reddit search: no results from any fallback. {BLOCKED_HINT}"
            ) from blocked
        return [post_hit(ch.get("data", {})) for ch in data.get("data", {}).get("children", [])
                if ch.get("kind") == "t3"][:limit]  # fmt: skip

    async def _rss_search(self, query, limit, since, sort="relevance"):
        """Live search via the .rss endpoint, which stays open when .json is blocked."""
        params = urlencode({"q": query, "sort": sort, "t": rss_window(since), "type": "link",
                            "limit": min(limit * 2, 25)})  # fmt: skip
        f = await self.fetch(f"{WWW}/search.rss?{params}", archive=False)
        if not f.ok:
            raise WireFetchError(f"reddit search.rss: HTTP {f.status}")
        hits = [
            h for h in rss_hits(f.content) if not since or not h.timestamp or h.timestamp >= since
        ]
        return hits[:limit]

    async def _pullpush_search(self, query, limit, since, sort="relevance"):
        params = {"q": query, "size": min(limit * 2, 25), "sort_type": "score", "sort": "desc"}
        if since:
            params["after"] = int(since.timestamp())
        data, _ = await self.fetch_json(f"{PULLPUSH}?{urlencode(params)}")
        posts = data.get("data") if isinstance(data, dict) else None
        if not isinstance(posts, list):
            raise WireFetchError("pullpush: no data")
        return [post_hit(p, archived=True) for p in posts if p.get("permalink")][:limit]

    # ---- Arctic Shift fallback --------------------------------------------------------------
    async def _arctic(self, path: str, **params):
        url = f"{ARCTIC}{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        data: dict = {}
        for attempt in range(2):
            try:
                data, f = await self.fetch_json(url, archive=False)
            except ValueError as e:  # HTML error page instead of JSON
                raise WireFetchError(f"arctic-shift unavailable. {BLOCKED_HINT}") from e
            if isinstance(data, dict) and data.get("data") is not None:
                return data["data"], f
            if attempt == 0:  # "Timeout. Maybe slow down a bit": one polite retry
                await asyncio.sleep(3)
        raise WireFetchError(f"arctic-shift: {data.get('error') or 'no data'} ({BLOCKED_HINT})")

    async def _arctic_thread(self, url: str, post_id: str):
        posts, f = await self._arctic("/posts/ids", ids=post_id)
        if not posts:
            raise WireFetchError(f"{url}: not in the Arctic Shift archive. {BLOCKED_HINT}")
        tree, _ = await self._arctic("/comments/tree", link_id=f"t3_{post_id}", limit=300)
        data = [{"data": {"children": [{"kind": "t3", "data": posts[0]}]}},
                {"data": {"children": tree}}]  # fmt: skip
        fields = parse_thread(data)
        fields["metadata"]["source"] = ARCTIC_NOTE
        return self.doc(f, url, **fields)

    async def subreddits_for(self, query: str, n: int = 3) -> list[str]:
        """Dedicated-subreddit discovery: subreddits whose names start with a topic word."""
        words = [
            w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in _STOP
        ]
        if m := re.findall(r"\br/(\w+)", query):
            return m[:n]
        found: dict[str, int] = {}
        for w in words[:3]:
            try:
                subs, _ = await self._arctic("/subreddits/search", subreddit_prefix=w, limit=5)
            except WireFetchError:
                continue
            for sub in subs:
                name = sub.get("display_name") or ""
                if name:
                    found[name] = max(found.get(name, 0), sub.get("subscribers") or 0)
        return sorted(found, key=lambda k: -found[k])[:n]

    async def _arctic_search(self, query, limit, since: datetime | None, sort="relevance"):
        subs = await self.subreddits_for(query)
        if not subs:
            raise WireFetchError(f"reddit search: no matching subreddits found. {BLOCKED_HINT}")
        words = [w for w in re.findall(r"[A-Za-z0-9+#.]+", re.sub(r"\br/\w+", "", query))
                 if w.lower() not in _STOP]  # fmt: skip
        after = since.date().isoformat() if since else None
        cap = min(limit * 2, 25)
        hits, seen = [], set()
        for sub in subs:
            # every topic word (title search ANDs them), then the word that named this subreddit:
            # r/rust is about rust, so "rust programming" there only needs "rust"
            tries = [" ".join(words)] if words else [None]
            own = [w for w in words if sub.lower().startswith(w.lower())]
            if own and own[0] != tries[0]:
                tries.append(own[0])
            for title in tries:
                try:
                    posts, _ = await self._arctic("/posts/search", subreddit=sub, title=title,
                                                  after=after, limit=cap, sort="desc")  # fmt: skip
                except WireFetchError:
                    break
                for p in posts:
                    if p.get("id") not in seen:
                        seen.add(p.get("id"))
                        hits.append(post_hit(p, archived=True))
                if len(posts) >= 3:
                    break
        hits.sort(key=lambda h: -h.rank_score)
        return hits[:limit]

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().strip("/").removeprefix("https://www.reddit.com/")
        if t.startswith(("u/", "user/", "@")):
            user = t.split("/", 1)[-1].lstrip("@")
            path, title = f"/user/{user}/submitted", f"u/{user}"
        else:
            sub = t.removeprefix("r/").split("/")[0]
            path, title = f"/r/{sub}/{sort}", f"r/{sub} · {sort}"
        try:
            data, f = await self._json(path, f"limit={limit}")
        except WireFetchError:
            who = {"author": user} if t.startswith(("u/", "user/", "@")) else {"subreddit": sub}
            posts, f = await self._arctic("/posts/search", limit=limit, sort="desc", **who)
            data = {"data": {"children": [{"kind": "t3", "data": p} for p in posts]}}
            title += f" · {ARCTIC_NOTE}"
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
