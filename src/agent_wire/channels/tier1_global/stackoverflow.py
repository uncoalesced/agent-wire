"""StackOverflow / StackExchange: question, accepted answer and top-voted answers via API 2.3."""

import html
import re
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, html_to_markdown, ts
from agent_wire.models import WireComment, WireSearchHit

API = "https://api.stackexchange.com/2.3"
TOP_ANSWERS = 3
_ROOT_SITES = {"stackoverflow.com": "stackoverflow", "superuser.com": "superuser",
               "serverfault.com": "serverfault", "askubuntu.com": "askubuntu",
               "mathoverflow.net": "mathoverflow.net"}  # fmt: skip


def site_for(host: str) -> str:
    host = host.removeprefix("www.")
    if host in _ROOT_SITES:
        return _ROOT_SITES[host]
    return host.removesuffix(".stackexchange.com")


def pick_answers(answers: list[dict]) -> list[dict]:
    """Accepted answer plus the top-voted others, in vote order."""
    ranked = sorted(answers, key=lambda a: -(a.get("score") or 0))
    accepted = [a for a in ranked if a.get("is_accepted")]
    others = [a for a in ranked if not a.get("is_accepted")][:TOP_ANSWERS]
    return accepted + others


def _owner(item: dict) -> str | None:
    return html.unescape((item.get("owner") or {}).get("display_name") or "") or None


class StackOverflowChannel(Channel):
    name = "stackoverflow"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        parts = urlsplit(url)
        m = re.search(r"/(?:questions|q)/(\d+)", parts.path)
        if not m:
            return await self.ctx.channel("web").read(url, query=query, section=section)
        qid, site = m[1], site_for(parts.hostname or "stackoverflow.com")
        q_data, f = await self.fetch_json(f"{API}/questions/{qid}?site={site}&filter=withbody")
        if not q_data.get("items"):
            return await self.ctx.channel("web").read(url, query=query, section=section)
        q = q_data["items"][0]
        comments = []
        if include_comments:
            a_data, _ = await self.fetch_json(
                f"{API}/questions/{qid}/answers?site={site}&filter=withbody&sort=votes&pagesize=20"
            )
            comments = [
                WireComment(
                    id=str(a["answer_id"]), body=html_to_markdown(a.get("body") or ""),
                    author_handle=_owner(a), score=a.get("score") or 0,
                    is_accepted=bool(a.get("is_accepted")), timestamp=ts(a.get("creation_date")),
                )
                for a in pick_answers(a_data.get("items", []))
            ]  # fmt: skip
        body = html_to_markdown(q.get("body") or "")
        return self.doc(
            f, url, title=html.unescape(q.get("title", "")), author_handle=_owner(q),
            timestamp=ts(q.get("creation_date")), score=q.get("score"), body=body,
            comments=comments,
            metadata={"tags": ", ".join(q.get("tags") or []), "answers": q.get("answer_count"),
                      "views": q.get("view_count"), "answered": q.get("is_answered")},
        )  # fmt: skip

    async def search(self, query, *, limit=5, sort="relevance", since=None):
        order = {"top": "votes", "new": "creation"}.get(sort, "relevance")
        window = f"&fromdate={int(since.timestamp())}" if since else ""
        data, _ = await self.fetch_json(
            f"{API}/search/advanced?q={quote(query)}&site=stackoverflow&sort={order}&order=desc"
            f"&pagesize={limit}{window}"
        )
        return [
            WireSearchHit(
                url=it["link"], title=html.unescape(it.get("title", "")),
                snippet=f"score {it.get('score', 0)} · {it.get('answer_count', 0)} answers"
                        + (" · accepted" if it.get("accepted_answer_id") else "")
                        + f" · {', '.join(it.get('tags', [])[:5])}",
                source="stackoverflow", rank_score=float(it.get("score") or 0),
                timestamp=ts(it.get("creation_date")),
                author=(it.get("owner") or {}).get("display_name"),
                engagement={"score": it.get("score") or 0, "comments": it.get("answer_count") or 0,
                            "views": it.get("view_count") or 0},
            )
            for it in data.get("items", [])[:limit]
        ]  # fmt: skip


CHANNEL = StackOverflowChannel
