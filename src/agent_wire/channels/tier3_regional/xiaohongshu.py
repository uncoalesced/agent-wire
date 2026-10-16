"""Xiaohongshu (RED): note body, stats, tags and any server-rendered comments from __INITIAL_STATE__."""

import re
from urllib.parse import urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, assigned_json, compact_count, ts
from agent_wire.models import WireComment

_NOTE = re.compile(r"/(?:explore|discovery/item|search_result)/([0-9a-f]{24})")


def parse_note(state: dict, note_id: str) -> dict:
    detail = ((state.get("note") or {}).get("noteDetailMap") or {}).get(note_id) or {}
    note = detail.get("note") or {}
    if not note:
        raise WireUnsupported("xiaohongshu note not in page state (needs xsec_token or a login)")
    inter = note.get("interactInfo") or {}
    comments = [
        WireComment(id=str(c.get("id")), body=c.get("content", ""),
                    author_handle=(c.get("userInfo") or {}).get("nickname"),
                    score=compact_count(c.get("likeCount")), timestamp=ts(c.get("createTime")))
        for c in ((detail.get("comments") or {}).get("list") or [])
    ]  # fmt: skip
    tags = " ".join(f"#{t.get('name')}" for t in note.get("tagList") or [])
    images = len(note.get("imageList") or [])
    body = (note.get("desc") or "") + (f"\n\n{tags}" if tags else "")
    return {
        "title": note.get("title") or (note.get("desc") or "")[:40],
        "author_handle": (note.get("user") or {}).get("nickname"),
        "timestamp": ts(note.get("time")), "score": compact_count(inter.get("likedCount")),
        "body": body, "comments": comments,
        "metadata": {"likes": compact_count(inter.get("likedCount")),
                     "collects": compact_count(inter.get("collectedCount")),
                     "comments": compact_count(inter.get("commentCount")),
                     "type": note.get("type"), "images": images or None},
    }  # fmt: skip


class XiaohongshuChannel(Channel):
    name = "xiaohongshu"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        f = await self.fetch(
            url, archive=False
        )  # login-only channel: local session attached if present
        m = _NOTE.search(urlsplit(f.url).path) or _NOTE.search(urlsplit(url).path)
        if not m:
            raise WireUnsupported("xiaohongshu: expected an /explore/<note id> URL")
        state = assigned_json(f.text, "window.__INITIAL_STATE__", js_undefined=True) or {}
        return self.doc(f, url, **parse_note(state, m[1]))

    async def search(self, query, *, limit=5, sort="relevance"):
        raise WireUnsupported(
            "xiaohongshu search needs its signed web API; share a note URL instead"
        )


CHANNEL = XiaohongshuChannel
