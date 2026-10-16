"""X / Twitter: zero-cookie single posts (fxtwitter, vxtwitter, syndication); GraphQL for the rest."""

import math
import re
from urllib.parse import quote, unquote, urlsplit

import orjson
from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireComment, WireSearchHit

# The public web-app bearer token embedded in X's own JavaScript (not a user secret).
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
FEATURES = {
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}
_STATUS = re.compile(r"/(?:i/web/)?(?:(\w+)/)?status(?:es)?/(\d+)")
_MISSING_FEATURES = re.compile(r"features cannot be null: (.+)", re.IGNORECASE)
_MAIN_JS = re.compile(
    r"https://abs\.twimg\.com/responsive-web/client-web(?:-legacy)?/main\.[\w]+\.js"
)
_QUERY_ID = re.compile(r'queryId:"([\w-]+)",operationName:"(\w+)"')


def syndication_token(tweet_id: str) -> str:
    """Python port of X embed's `((id / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '')`."""
    x = int(tweet_id) / 1e15 * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole, frac = int(x), x - int(x)
    head = ""
    while whole:
        head, whole = digits[whole % 36] + head, whole // 36
    tail = ""
    for _ in range(10):
        frac *= 36
        tail += digits[int(frac)]
        frac -= int(frac)
    return re.sub(r"(0+|\.)", "", f"{head or '0'}.{tail}")


def normalize(data: dict, source: str) -> dict:
    """fxtwitter / vxtwitter / syndication payload -> one tweet shape."""
    if source == "fxtwitter":
        t = data.get("tweet") or {}
        quote_t = t.get("quote")
        return {
            "id": t.get("id"), "text": t.get("text", ""),
            "author": (t.get("author") or {}).get("screen_name"),
            "likes": t.get("likes"), "retweets": t.get("retweets"), "replies": t.get("replies"),
            "views": t.get("views"), "created": ts(t.get("created_timestamp")),
            "media": [m.get("altText") or m.get("type") for m in (t.get("media") or {}).get("all", [])],
            "quote": normalize({"tweet": quote_t}, "fxtwitter") if quote_t else None,
        }  # fmt: skip
    if source == "vxtwitter":
        return {
            "id": data.get("tweetID"), "text": data.get("text", ""),
            "author": data.get("user_screen_name"), "likes": data.get("likes"),
            "retweets": data.get("retweets"), "replies": data.get("replies"), "views": None,
            "created": ts(data.get("date_epoch")),
            "media": [m.get("altText") or m.get("type") for m in data.get("media_extended") or []],
            "quote": normalize(data["qrt"], "vxtwitter") if data.get("qrt") else None,
        }  # fmt: skip
    return {  # syndication
        "id": data.get("id_str"), "text": data.get("text", ""),
        "author": (data.get("user") or {}).get("screen_name"),
        "likes": data.get("favorite_count"), "retweets": data.get("retweet_count"),
        "replies": data.get("conversation_count"), "views": None,
        "created": ts(data.get("created_at")),
        "media": [m.get("ext_alt_text") or m.get("type") for m in data.get("mediaDetails") or []],
        "quote": normalize(data["quoted_tweet"], "syndication") if data.get("quoted_tweet") else None,
    }  # fmt: skip


def render_tweet(t: dict) -> str:
    parts = [t["text"]]
    if media := [m for m in t["media"] if m]:
        parts.append("Media: " + "; ".join(media))
    if q := t.get("quote"):
        parts.append(f"> Quoting @{q['author']}: " + q["text"].replace("\n", "\n> "))
    return "\n\n".join(parts)


def graphql_tweets(data) -> list[dict]:
    """Every tweet in a GraphQL timeline, in order, deduped."""
    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            res = o.get("tweet_results", {}).get("result") if "tweet_results" in o else None
            if res:
                res = res.get("tweet", res)  # TweetWithVisibilityResults wrapper
                legacy = res.get("legacy") or {}
                if legacy.get("full_text") is not None and res.get("rest_id") not in seen:
                    seen.add(res.get("rest_id"))
                    user = ((res.get("core") or {}).get("user_results") or {}).get("result") or {}
                    name = (user.get("core") or {}).get("screen_name") or (
                        user.get("legacy") or {}
                    ).get("screen_name")
                    note = ((res.get("note_tweet") or {}).get("note_tweet_results") or {}).get(
                        "result"
                    ) or {}
                    out.append({
                        "id": res.get("rest_id"), "author": name,
                        "text": note.get("text") or legacy["full_text"],
                        "likes": legacy.get("favorite_count"), "retweets": legacy.get("retweet_count"),
                        "replies": legacy.get("reply_count"),
                        "views": (res.get("views") or {}).get("count"),
                        "created": ts(_twitter_date(legacy.get("created_at"))),
                        "reply_to": legacy.get("in_reply_to_status_id_str"),
                    })  # fmt: skip
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def _twitter_date(value):
    from email.utils import parsedate_to_datetime

    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def tweet_items(tweets: list[dict]) -> list[dict]:
    return [{"title": f"@{t['author']}: {t['text'][:120]}",
             "url": f"https://x.com/{t['author']}/status/{t['id']}",
             "meta": [f"♥{t.get('likes') or 0}", f"↻{t.get('retweets') or 0}",
                      f"💬{t.get('replies') or 0}",
                      f"{t['views']} views" if t.get("views") else None,
                      t["created"].date().isoformat() if t.get("created") else None],
             "snippet": t["text"] if len(t["text"]) > 120 else ""} for t in tweets]  # fmt: skip


def parse_syndication_timeline(html: str) -> list[dict]:
    node = LexborHTMLParser(html).css_first("script#__NEXT_DATA__")
    if not node:
        return []
    data = orjson.loads(node.text())
    entries = data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
    out = []
    for e in entries:
        t = (e.get("content") or {}).get("tweet")
        if t:
            out.append({
                "id": t.get("id_str"), "author": (t.get("user") or {}).get("screen_name"),
                "text": t.get("full_text") or t.get("text", ""), "likes": t.get("favorite_count"),
                "retweets": t.get("retweet_count"), "replies": t.get("reply_count"), "views": None,
                "created": ts(_twitter_date(t.get("created_at"))),
            })  # fmt: skip
    return out


class XChannel(Channel):
    name = "x"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._query_ids: dict[str, str] = {}
        self._guest: str | None = None

    # ---- single posts (no cookies) ---------------------------------------------------------
    async def read(self, url, *, query=None, section=None, include_comments=True):
        m = _STATUS.search(urlsplit(url).path)
        if not m:
            handle = urlsplit(url).path.strip("/").split("/")[0]
            if handle:
                return await self.feed(f"@{handle}")
            raise WireUnsupported("x: expected a /<user>/status/<id> or profile URL")
        user, tid = m[1] or "i", m[2]
        primary = f"https://api.fxtwitter.com/{user}/status/{tid}"
        mirrors = [f"https://api.vxtwitter.com/{user}/status/{tid}",
                   f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token={syndication_token(tid)}"]  # fmt: skip
        data, f = await self.fetch_json(primary, mirrors=mirrors)
        host = urlsplit(f.requested or primary).hostname or ""
        source = (
            "fxtwitter"
            if "fxtwitter" in host
            else "vxtwitter"
            if "vxtwitter" in host
            else "syndication"
        )
        t = normalize(data, source)
        if not t["text"] and not t["author"]:
            raise WireFetchError(f"{url}: post unavailable (deleted, private or age-gated)")
        replies = await self._replies(tid, t["author"]) if include_comments else []
        return self.doc(
            f, f"https://x.com/{t['author'] or user}/status/{tid}",
            title=f"@{t['author']} on X", author_handle=t["author"], timestamp=t["created"],
            score=t["likes"], body=render_tweet(t), comments=replies,
            metadata={"likes": t["likes"], "retweets": t["retweets"], "replies": t["replies"],
                      "views": t["views"], "via": source},
        )  # fmt: skip

    async def _replies(self, tid: str, author: str | None) -> list[WireComment]:
        try:
            data = await self.graphql("TweetDetail", {
                "focalTweetId": tid, "with_rux_injections": False, "includePromotedContent": False,
                "withCommunity": True, "withVoice": True, "withV2Timeline": True,
                "withBirdwatchNotes": False, "withQuickPromoteEligibilityTweetFields": False,
            }, cookies=self._vault_only())  # fmt: skip
        except (WireFetchError, WireUnsupported, ValueError, KeyError):
            return []  # replies need a guest token X may refuse; the post itself still reads
        return [
            WireComment(id=t["id"], body=t["text"], author_handle=t["author"],
                        score=t.get("likes") or 0, timestamp=t["created"], is_op=t["author"] == author)
            for t in graphql_tweets(data) if t["id"] != tid and t.get("reply_to") == tid
        ]  # fmt: skip

    def _vault_only(self) -> dict | None:
        """Saved `wire auth login x.com` session, if any (the only cookies agent-wire ever uses)."""
        vault = self.ctx.vault
        jar = vault.get("x.com") if vault else {}
        return jar if jar.get("auth_token") and jar.get("ct0") else None

    # ---- GraphQL -----------------------------------------------------------------------------
    async def _ids(self) -> dict[str, str]:
        if not self._query_ids:
            home = await self.fetch("https://x.com/", archive=False, browser=False)
            m = _MAIN_JS.search(home.text)
            if not m:
                raise WireUnsupported("x: couldn't locate the web client bundle for GraphQL ids")
            js = await self.fetch(m[0], archive=False, browser=False)
            self._query_ids = {op: qid for qid, op in _QUERY_ID.findall(js.text)}
        return self._query_ids

    async def _guest_token(self) -> str:
        if not self._guest:
            f = await self.ctx.pool.get(
                "https://api.x.com/1.1/guest/activate.json", method="POST", impersonate="chrome",
                headers={"authorization": f"Bearer {BEARER}"},
            )  # fmt: skip
            self._guest = parse_json(f.content).get("guest_token")
            if not self._guest:
                raise WireUnsupported("x refused a guest token")
        return self._guest

    async def graphql(self, op: str, variables: dict, cookies: dict | None = None) -> dict:
        qid = (await self._ids()).get(op)
        if not qid:
            raise WireUnsupported(f"x GraphQL operation {op} not found in the web bundle")
        headers = {"authorization": f"Bearer {BEARER}", "x-twitter-active-user": "yes",
                   "content-type": "application/json"}  # fmt: skip
        if cookies:
            headers.update({"x-csrf-token": cookies["ct0"], "x-twitter-auth-type": "OAuth2Session"})
            if self.ctx.waterfall.limiter:
                await self.ctx.waterfall.limiter.wait("x.com")
        else:
            headers["x-guest-token"] = await self._guest_token()
        features = dict(FEATURES)
        for _ in range(3):  # X lists any newly-required feature flags in a 400; add them and retry
            url = (f"https://x.com/i/api/graphql/{qid}/{op}?variables={quote(orjson.dumps(variables).decode())}"
                   f"&features={quote(orjson.dumps(features).decode())}")  # fmt: skip
            f = await self.ctx.pool.get(url, headers=headers, cookies=cookies, impersonate="chrome")
            data = parse_json(f.content) if f.content.strip().startswith(b"{") else {}
            missing = [
                m
                for e in data.get("errors", [])
                if (m := _MISSING_FEATURES.search(e.get("message", "")))
            ]
            if f.status == 400 and missing:
                for name in re.split(r",\s*", missing[0][1]):
                    features[name.strip()] = True
                continue
            if not f.ok:
                raise WireFetchError(f"x GraphQL {op}: HTTP {f.status}")
            return data
        raise WireFetchError(f"x GraphQL {op}: feature negotiation failed")

    async def _session(self, purpose: str) -> dict:
        jar = await self.ctx.vault.cookies_for("x.com") if self.ctx.vault else {}
        if not (jar.get("auth_token") and jar.get("ct0")):
            raise WireUnsupported(
                f"{purpose} needs a logged-in X session: run `wire auth login x.com`"
            )
        return jar

    # ---- search / feed / me ----------------------------------------------------------------
    async def search(self, query, *, limit=5, sort="relevance"):
        jar = await self._session("X search")
        data = await self.graphql("SearchTimeline", {
            "rawQuery": query, "count": max(limit, 10), "querySource": "typed_query",
            "product": "Latest" if sort in ("new", "latest") else "Top",
        }, cookies=jar)  # fmt: skip
        return [
            WireSearchHit(url=f"https://x.com/{t['author']}/status/{t['id']}", title=f"@{t['author']}",
                          snippet=t["text"], source="x", rank_score=float(t.get("likes") or 0))
            for t in graphql_tweets(data)[:limit]
        ]  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        handle = target.strip().lstrip("@").split("/")[-1]
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
        try:
            f = await self.fetch(url, archive=False, browser=False)
            tweets = parse_syndication_timeline(f.text)
        except WireFetchError:
            f, tweets = None, []
        if not tweets:  # syndication is often empty now; fall back to GraphQL with a session
            jar = await self._session(f"@{handle}'s timeline")
            user = await self.graphql("UserByScreenName", {"screen_name": handle}, cookies=jar)
            uid = user.get("data", {}).get("user", {}).get("result", {}).get("rest_id")
            data = await self.graphql("UserTweets", {"userId": uid, "count": limit,
                                                     "includePromotedContent": False, "withVoice": True,
                                                     "withV2Timeline": True}, cookies=jar)  # fmt: skip
            tweets = graphql_tweets(data)
        return self.doc(f, f"https://x.com/{handle}", title=f"@{handle} on X",
                        body=listing(tweet_items(tweets[:limit])))  # fmt: skip

    async def me(self, target="latest_posts", *, limit=5):
        jar = await self._session("wire_me x")
        uid = unquote(jar.get("twid", "")).removeprefix("u=")
        if not uid.isdigit():
            raise WireUnsupported("x session has no twid cookie; re-run `wire auth login x.com`")
        common = {"includePromotedContent": False, "withVoice": True, "withV2Timeline": True}
        if target == "notifications":
            if self.ctx.waterfall.limiter:
                await self.ctx.waterfall.limiter.wait("x.com")
            f = await self.ctx.pool.get(
                f"https://x.com/i/api/2/notifications/all.json?count={limit * 4}", cookies=jar,
                headers={"authorization": f"Bearer {BEARER}", "x-csrf-token": jar["ct0"],
                         "x-twitter-auth-type": "OAuth2Session"},
            )  # fmt: skip
            notes = parse_json(f.content).get("globalObjects", {}).get("notifications", {})
            items = [{"title": n.get("message", {}).get("text", ""), "url": "https://x.com/notifications",
                      "meta": [ts(n.get("timestampMs"))]} for n in list(notes.values())[:limit]]  # fmt: skip
            return self.doc(
                f, "https://x.com/notifications", title="my X notifications", body=listing(items)
            )
        ops = {"latest_posts": ("UserTweets", {"userId": uid, "count": limit, **common}),
               "likes": ("Likes", {"userId": uid, "count": limit, **common}),
               "bookmarks": ("Bookmarks", {"count": limit, "includePromotedContent": False}),
               "stats": ("UserByRestId", {"userId": uid})}  # fmt: skip
        if target not in ops:
            raise WireUnsupported(f"x targets: {', '.join([*ops, 'notifications'])}")
        op, variables = ops[target]
        data = await self.graphql(op, variables, cookies=jar)
        if target == "stats":
            legacy = data.get("data", {}).get("user", {}).get("result", {}).get("legacy", {})
            body = (f"{legacy.get('followers_count')} followers · {legacy.get('friends_count')} following"
                    f" · {legacy.get('statuses_count')} posts")  # fmt: skip
        else:
            tweets = graphql_tweets(data)
            if target == "latest_posts":
                tweets = [t for t in tweets if not t.get("reply_to")] or tweets
            body = listing(tweet_items(tweets[:limit]))
        return self.doc(
            None, "https://x.com/home", title=f"my X {target}", body=body, auth="cookie"
        )


CHANNEL = XChannel
