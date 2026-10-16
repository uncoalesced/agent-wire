"""V2EX: native public API for topics, replies and hot/latest/node feeds; SoV2EX for search."""

import re
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.models import WireComment, WireSearchHit

API = "https://www.v2ex.com/api"


def parse_replies(replies: list[dict], op: str | None) -> list[WireComment]:
    return [WireComment(id=str(r.get("id")), body=r.get("content") or "",
                        author_handle=(r.get("member") or {}).get("username"),
                        timestamp=ts(r.get("created")), is_op=(r.get("member") or {}).get("username") == op)
            for r in replies]  # fmt: skip


def topic_items(topics: list[dict]) -> list[dict]:
    return [{"title": t.get("title"), "url": t.get("url"),
             "meta": [(t.get("node") or {}).get("title"), f"{t.get('replies', 0)} replies",
                      (t.get("member") or {}).get("username")]} for t in topics]  # fmt: skip


class V2exChannel(Channel):
    name = "v2ex"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        m = re.search(r"/t/(\d+)", urlsplit(url).path)
        if not m:
            node = re.search(r"/go/([\w-]+)", urlsplit(url).path)
            if node:
                return await self.feed(node[1])
            raise WireUnsupported("v2ex: expected /t/<id> or /go/<node>")
        topics, f = await self.fetch_json(f"{API}/topics/show.json?id={m[1]}")
        if not topics:
            raise WireUnsupported(f"v2ex topic {m[1]} not found")
        t = topics[0]
        op = (t.get("member") or {}).get("username")
        comments = []
        if include_comments and t.get("replies"):
            replies, _ = await self.fetch_json(f"{API}/replies/show.json?topic_id={m[1]}")
            comments = parse_replies(replies, op)
        return self.doc(f, url, title=t.get("title"), author_handle=op, timestamp=ts(t.get("created")),
                        body=t.get("content") or "", comments=comments,
                        metadata={"node": (t.get("node") or {}).get("title"), "replies": t.get("replies")})  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().lower() or "hot"
        path = {"hot": "topics/hot.json", "latest": "topics/latest.json"}.get(
            t, f"topics/show.json?node_name={t}"
        )
        topics, f = await self.fetch_json(f"{API}/{path}")
        return self.doc(
            f,
            f"https://www.v2ex.com/?tab={t}",
            title=f"V2EX · {t}",
            body=listing(topic_items(topics[:limit])),
        )

    async def search(self, query, *, limit=5, sort="relevance"):
        data, _ = await self.fetch_json(
            f"https://www.sov2ex.com/api/search?q={quote(query)}&size={limit}"
        )
        return [WireSearchHit(url=f"https://www.v2ex.com/t/{h['_source']['id']}", title=h["_source"].get("title", ""),
                              snippet=(h["_source"].get("content") or "")[:200], source="v2ex")
                for h in data.get("hits", [])]  # fmt: skip


CHANNEL = V2exChannel
