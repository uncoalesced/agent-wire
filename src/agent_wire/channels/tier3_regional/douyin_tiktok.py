"""TikTok (rehydration JSON + WebVTT captions, oEmbed fallback) and Douyin (iesdouyin share page)."""

import re
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, assigned_json, script_json, ts
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.media.subtitle_cleaner import parse_captions, to_paragraphs

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")  # fmt: skip


def parse_tiktok(data: dict) -> dict:
    scope = data.get("__DEFAULT_SCOPE__") or {}
    item = ((scope.get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct") or {}
    if not item:
        raise WireUnsupported("tiktok video data missing (removed, private or region-blocked)")
    stats = item.get("stats") or {}
    subs = (item.get("video") or {}).get("subtitleInfos") or []
    return {"title": item.get("desc", "")[:80], "author_handle": (item.get("author") or {}).get("uniqueId"),
            "timestamp": ts(item.get("createTime")), "score": stats.get("diggCount"), "body": item.get("desc", ""),
            "metadata": {"plays": stats.get("playCount"), "likes": stats.get("diggCount"),
                         "comments": stats.get("commentCount"), "shares": stats.get("shareCount")},
            "_subs": subs}  # fmt: skip


def parse_douyin(data: dict) -> dict:
    loader = (data or {}).get("loaderData") or {}
    page = next((v for k, v in loader.items() if "video" in k and isinstance(v, dict)), {})
    items = ((page.get("videoInfoRes") or {}).get("item_list")) or []
    if not items:
        raise WireUnsupported("douyin video data missing from the share page")
    it = items[0]
    st = it.get("statistics") or {}
    return {"title": it.get("desc", "")[:80], "author_handle": (it.get("author") or {}).get("nickname"),
            "timestamp": ts(it.get("create_time")), "score": st.get("digg_count"), "body": it.get("desc", ""),
            "metadata": {"likes": st.get("digg_count"), "comments": st.get("comment_count"),
                         "shares": st.get("share_count"), "collects": st.get("collect_count")}}  # fmt: skip


class DouyinTikTokChannel(Channel):
    name = "douyin_tiktok"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        host = urlsplit(url).hostname or ""
        if "tiktok.com" in host:
            return await self._tiktok(url)
        if host.startswith("v.douyin.com"):
            url = (await self.fetch(url, archive=False, browser=False)).url
        m = re.search(r"/(?:video|note)/(\d+)", urlsplit(url).path)
        if not m:
            raise WireUnsupported("douyin: expected a /video/<id> URL")
        f = await self.fetch(f"https://www.iesdouyin.com/share/video/{m[1]}/",
                             headers={"User-Agent": MOBILE_UA}, archive=False)  # fmt: skip
        data = assigned_json(f.text, "window._ROUTER_DATA", js_undefined=True)
        return self.doc(f, url, **parse_douyin(data))

    async def _tiktok(self, url):
        try:
            f = await self.fetch(url, archive=False)
            info = parse_tiktok(
                script_json(f.text, "script#__UNIVERSAL_DATA_FOR_REHYDRATION__") or {}
            )
        except (WireFetchError, WireUnsupported):
            data, f = await self.fetch_json(
                f"https://www.tiktok.com/oembed?url={quote(url, safe='')}"
            )
            return self.doc(f, url, title=data.get("title"), author_handle=data.get("author_unique_id"),
                            body=data.get("title", ""), metadata={"via": "oembed"})  # fmt: skip
        subs = info.pop("_subs")
        sub = next(
            (s for s in subs if str(s.get("LanguageCodeName", "")).startswith("en")),
            subs[0] if subs else None,
        )
        if sub and sub.get("Url"):
            try:
                vtt = await self.fetch(sub["Url"], archive=False, browser=False)
                info["body"] += "\n\n## Captions\n" + to_paragraphs(parse_captions(vtt.content))
            except (WireFetchError, ValueError):
                pass
        return self.doc(f, url, **info)


CHANNEL = DouyinTikTokChannel
