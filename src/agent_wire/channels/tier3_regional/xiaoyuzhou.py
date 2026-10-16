"""Xiaoyuzhou (小宇宙) podcasts: episode show notes, audio URL, optional transcript; podcast feeds."""

import re
from urllib.parse import urlsplit

from agent_wire.channels.base import (
    Channel,
    WireUnsupported,
    html_to_markdown,
    listing,
    script_json,
    ts,
)
from agent_wire.media import audio_transcriber


def page_props(html: str) -> dict:
    data = script_json(html, "script#__NEXT_DATA__") or {}
    return (data.get("props") or {}).get("pageProps") or {}


def parse_episode(props: dict) -> dict:
    ep = props.get("episode") or {}
    if not ep:
        raise WireUnsupported("xiaoyuzhou episode data missing")
    notes = ep.get("shownotes") or ep.get("description") or ""
    audio = (ep.get("enclosure") or {}).get("url") or (ep.get("media") or {}).get("source", {}).get(
        "url"
    )
    return {"title": ep.get("title"), "author_handle": (ep.get("podcast") or {}).get("title"),
            "timestamp": ts(ep.get("pubDate")), "body": html_to_markdown(notes) if "<" in notes else notes,
            "metadata": {"length": f"{(ep.get('duration') or 0) // 60}m", "plays": ep.get("playCount"),
                         "comments": ep.get("commentCount"), "audio": audio}}  # fmt: skip


class XiaoyuzhouChannel(Channel):
    name = "xiaoyuzhou"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        if re.search(r"/podcast/", urlsplit(url).path):
            return await self.feed(url)
        f = await self.fetch(url, archive=False)
        info = parse_episode(page_props(f.text))
        audio = info["metadata"].get("audio")
        if audio and audio_transcriber.enabled():
            try:
                transcript = await audio_transcriber.transcribe(self.ctx.pool, audio)
                info["body"] += f"\n\n## Transcript\n{transcript}"
            except audio_transcriber.TranscriptionUnavailable as e:
                info["body"] += f"\n\n_Transcript unavailable: {e}_"
        return self.doc(f, url, **info)

    async def feed(self, target, *, sort="hot", limit=10):
        url = (
            target
            if target.startswith("http")
            else f"https://www.xiaoyuzhoufm.com/podcast/{target}"
        )
        f = await self.fetch(url, archive=False)
        props = page_props(f.text)
        pod = props.get("podcast") or {}
        episodes = pod.get("episodes") or props.get("episodes") or []
        items = [{"title": e.get("title"), "url": f"https://www.xiaoyuzhoufm.com/episode/{e.get('eid')}",
                  "meta": [(e.get("pubDate") or "")[:10], f"{(e.get('duration') or 0) // 60}m"],
                  "snippet": e.get("description") or ""} for e in episodes[:limit]]  # fmt: skip
        return self.doc(f, url, title=pod.get("title") or "Xiaoyuzhou podcast", author_handle=pod.get("author"),
                        body=(pod.get("brief") or "") + "\n\n" + listing(items))  # fmt: skip


CHANNEL = XiaoyuzhouChannel
