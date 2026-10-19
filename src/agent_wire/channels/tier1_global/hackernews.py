"""Hacker News (Algolia API) and Lobsters (.json) threads, search and front pages."""

import re
from urllib.parse import parse_qs, quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, listing, ts
from agent_wire.models import WireComment, WireSearchHit

ALGOLIA = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://news.ycombinator.com/item?id="
FEEDS = {"top": "front_page", "front_page": "front_page", "ask": "ask_hn", "show": "show_hn",
         "new": "story", "newest": "story", "jobs": "job"}  # fmt: skip


def _count(c: dict) -> int:
    return sum(1 + _count(k) for k in c.get("children") or [])


def parse_hn_comments(children: list, op: str | None, depth: int = 0) -> list[WireComment]:
    out = []
    for c in children or []:
        if not c.get("text") and not c.get("children"):
            continue  # deleted leaf
        out.append(
            WireComment(
                id=str(c["id"]), body=html_to_markdown(c.get("text") or "[deleted]"),
                author_handle=c.get("author"), timestamp=ts(c.get("created_at_i")),
                score=_count(c), score_kind="replies",  # HN hides comment points
                depth=depth, is_op=bool(op and c.get("author") == op),
                replies=parse_hn_comments(c.get("children"), op, depth + 1),
            )
        )  # fmt: skip
    return out


def parse_lobsters(data: dict) -> dict:
    nodes: dict[str, WireComment] = {}
    roots: list[WireComment] = []
    op = data.get("submitter_user") or {}
    op = op.get("username") if isinstance(op, dict) else op
    for c in data.get("comments", []):
        user = c.get("commenting_user")
        user = user.get("username") if isinstance(user, dict) else user
        node = WireComment(
            id=c["short_id"], body=html_to_markdown(c.get("comment") or ""), author_handle=user,
            score=c.get("score") or 0, depth=(c.get("depth") or 1) - 1,
            timestamp=ts(c.get("created_at")), is_op=user == op,
        )  # fmt: skip
        nodes[c["short_id"]] = node
        parent = nodes.get(c.get("parent_comment") or "")
        (parent.replies if parent else roots).append(node)
    body = html_to_markdown(data.get("description") or "")
    if data.get("url"):
        body = f"Link: {data['url']}\n\n{body}".strip()
    return {
        "title": data.get("title"), "author_handle": op, "score": data.get("score"),
        "timestamp": ts(data.get("created_at")), "body": body, "comments": roots,
        "metadata": {"tags": ", ".join(data.get("tags") or []) or None,
                     "comments": data.get("comment_count")},
    }  # fmt: skip


class HackerNewsChannel(Channel):
    name = "hackernews"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        parts = urlsplit(url)
        if (parts.hostname or "").endswith("lobste.rs"):
            m = re.match(r"/s/(\w+)", parts.path)
            page = re.fullmatch(r"/?(hottest|newest|active|t/[\w,]+)?/?", parts.path)
            if not m and page:  # front page, newest, active or a tag: its JSON listing
                data, f = await self.fetch_json(f"https://lobste.rs/{page[1] or 'hottest'}.json")
                items = [{"title": s.get("title"), "url": s.get("short_id_url") or s.get("comments_url"),
                          "meta": [f"▲{s.get('score', 0)}", f"{s.get('comment_count', 0)} comments",
                                   ", ".join(s.get("tags") or [])]} for s in data]  # fmt: skip
                return self.doc(
                    f, url, title=f"Lobsters · {page[1] or 'hottest'}", body=listing(items)
                )
            if not m:
                return await self.ctx.channel("web").read(url, query=query, section=section)
            data, f = await self.fetch_json(f"https://lobste.rs/s/{m[1]}.json")
            return self.doc(f, url, **parse_lobsters(data))
        item_id = parse_qs(parts.query).get("id", [""])[0]
        if not item_id.isdigit():
            return await self.ctx.channel("web").read(url, query=query, section=section)
        item, f = await self.fetch_json(f"{ALGOLIA}/items/{item_id}")
        body = html_to_markdown(item.get("text") or "")
        if item.get("url"):
            body = f"Link: {item['url']}\n\n{body}".strip()
        comments = (
            parse_hn_comments(item.get("children"), item.get("author")) if include_comments else []
        )
        return self.doc(
            f, url, title=item.get("title") or f"HN comment {item_id}",
            author_handle=item.get("author"), timestamp=ts(item.get("created_at_i")),
            score=item.get("points"), body=body, comments=comments,
            metadata={"comments": _count(item)},
        )  # fmt: skip

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        endpoint = "search_by_date" if sort in ("new", "date", "recent") else "search"
        window = f"&numericFilters=created_at_i>{int(since.timestamp())}" if since else ""
        data, _ = await self.fetch_json(
            f"{ALGOLIA}/{endpoint}?query={quote(query)}&tags=story&hitsPerPage={limit}{window}"
        )
        return [
            WireSearchHit(
                url=f"{HN_ITEM}{h['objectID']}", title=h.get("title") or "",
                snippet=f"{h.get('points', 0)} points · {h.get('num_comments', 0)} comments"
                        + (f" · {h['url']}" if h.get("url") else ""),
                source="hn", rank_score=float(h.get("points") or 0),
                timestamp=ts(h.get("created_at_i")), author=h.get("author"),
                engagement={"score": h.get("points") or 0, "comments": h.get("num_comments") or 0},
                link=h.get("url"),
            )
            for h in data.get("hits", [])[:limit]
        ]  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        key = target.strip().lower().removeprefix("hn/") or "top"
        if key not in FEEDS:
            raise WireUnsupported(f"hn feeds: {', '.join(FEEDS)}")
        endpoint = "search_by_date" if key in ("new", "newest", "jobs") else "search"
        data, f = await self.fetch_json(
            f"{ALGOLIA}/{endpoint}?tags={FEEDS[key]}&hitsPerPage={limit}"
        )
        items = [{"title": h.get("title"), "url": f"{HN_ITEM}{h['objectID']}",
                  "meta": [f"{h.get('points', 0)} pts", f"{h.get('num_comments', 0)} comments",
                           h.get("url")]} for h in data.get("hits", [])]  # fmt: skip
        return self.doc(
            f, "https://news.ycombinator.com/", title=f"Hacker News · {key}", body=listing(items)
        )


CHANNEL = HackerNewsChannel
