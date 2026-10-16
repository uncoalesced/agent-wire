"""Zhihu: questions with top answers, single answers and zhuanlan articles from js-initialData."""

import re
from urllib.parse import urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, script_json, ts
from agent_wire.models import WireComment


def parse_initial(state: dict, kind: str, ident: str, answer_id: str | None = None) -> dict:
    entities = (state.get("initialState") or {}).get("entities") or {}
    if kind == "article":
        a = (entities.get("articles") or {}).get(ident)
        if not a:
            raise WireUnsupported("zhihu article not in page state (login wall?)")
        return {"title": a.get("title"), "author_handle": (a.get("author") or {}).get("name"),
                "timestamp": ts(a.get("created")), "score": a.get("voteupCount"),
                "body": html_to_markdown(a.get("content") or "")}  # fmt: skip
    q = (entities.get("questions") or {}).get(ident) or {}
    answers = list((entities.get("answers") or {}).values())
    if answer_id:
        answers = [a for a in answers if str(a.get("id")) == answer_id] or answers
    if not q and not answers:
        raise WireUnsupported("zhihu question not in page state (login wall?)")
    comments = [
        WireComment(id=str(a.get("id")), body=html_to_markdown(a.get("content") or a.get("excerpt") or ""),
                    author_handle=(a.get("author") or {}).get("name"), score=a.get("voteupCount") or 0,
                    timestamp=ts(a.get("createdTime")))
        for a in answers
    ]  # fmt: skip
    return {"title": q.get("title"), "body": html_to_markdown(q.get("detail") or ""), "comments": comments,
            "metadata": {"answers": q.get("answerCount"), "followers": q.get("followerCount"),
                         "views": q.get("visitCount")}}  # fmt: skip


class ZhihuChannel(Channel):
    name = "zhihu"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        p = urlsplit(url)
        if (p.hostname or "").startswith("zhuanlan") and (m := re.search(r"/p/(\d+)", p.path)):
            kind, ident, answer = "article", m[1], None
        elif m := re.search(r"/question/(\d+)(?:/answer/(\d+))?", p.path):
            kind, ident, answer = "question", m[1], m[2]
        else:
            return await self.ctx.channel("web").read(url, query=query, section=section)
        f = await self.fetch(url, archive=False)
        state = script_json(f.text, "script#js-initialData") or {}
        return self.doc(f, url, **parse_initial(state, kind, ident, answer))

    async def search(self, query, *, limit=5, sort="relevance"):
        raise WireUnsupported(
            "zhihu search needs its signed API; use platforms=['web'] with 'site:zhihu.com'"
        )


CHANNEL = ZhihuChannel
