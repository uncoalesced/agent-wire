"""Bilibili: video info, CC subtitles, hot comments, search, popular feed — in-process WBI signing."""

import hashlib
import re
import time
from urllib.parse import urlencode, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, listing, ts
from agent_wire.engine.http_pool import Fetched
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.media.subtitle_cleaner import to_paragraphs
from agent_wire.models import WireComment, WireSearchHit

API = "https://api.bilibili.com"
HEADERS = {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
# Permutation table from Bilibili's web client for deriving the WBI mixin key.
MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19,
         29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
         22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]  # fmt: skip
KEY_TTL_S = 6 * 3600
_BVID = re.compile(r"(BV[0-9A-Za-z]{10})")
_TAG = re.compile(r"<[^>]+>")


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN)[:32]


def wbi_sign(params: dict, key: str, now: int | None = None) -> dict:
    signed = {**params, "wts": now if now is not None else int(time.time())}
    clean = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(signed.items())}
    query = urlencode(clean)
    return {**clean, "w_rid": hashlib.md5((query + key).encode()).hexdigest()}


def parse_replies(replies: list[dict], depth: int = 0) -> list[WireComment]:
    return [
        WireComment(id=str(r.get("rpid")), body=(r.get("content") or {}).get("message", ""),
                    author_handle=(r.get("member") or {}).get("uname"), score=r.get("like") or 0,
                    timestamp=ts(r.get("ctime")), depth=depth,
                    replies=parse_replies(r.get("replies") or [], depth + 1))
        for r in replies or []
    ]  # fmt: skip


class BilibiliChannel(Channel):
    name = "bilibili"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._key: str | None = None
        self._key_at = 0.0
        self._primed = False

    async def _get(self, url: str) -> Fetched:
        # One shared anonymous chrome session; the API answers 412 until the homepage has set the
        # buvid3/b_nut visitor cookies, so visit it once first.
        if not self._primed:
            await self.ctx.pool.get(
                "https://www.bilibili.com/", impersonate="chrome", headers=HEADERS
            )
            self._primed = True
        f = await self.ctx.pool.get(url, impersonate="chrome", headers=HEADERS)
        if not f.ok:
            raise WireFetchError(f"{url}: HTTP {f.status}")
        return f

    async def _api(self, path: str, params: dict, sign: bool = False) -> tuple[dict, Fetched]:
        if sign:
            params = wbi_sign(params, await self._wbi_key())
        f = await self._get(f"{API}{path}?{urlencode(params)}")
        data = parse_json(f.content)
        if data.get("code") not in (0, None):
            raise WireUnsupported(f"bilibili {path}: {data.get('message') or data.get('code')}")
        return data.get("data") or {}, f

    async def _wbi_key(self) -> str:
        if not self._key or time.time() - self._key_at > KEY_TTL_S:
            f = await self._get(f"{API}/x/web-interface/nav")
            img = parse_json(f.content)["data"]["wbi_img"]  # present even when logged out
            stem = lambda u: u.rsplit("/", 1)[-1].split(".")[0]
            self._key, self._key_at = (
                mixin_key(stem(img["img_url"]), stem(img["sub_url"])),
                time.time(),
            )
        return self._key

    async def read(self, url, *, query=None, section=None, include_comments=True):
        if (urlsplit(url).hostname or "").endswith("b23.tv"):
            url = (await self._get(url)).url  # short link -> video page
        m = _BVID.search(url)
        if not m:
            raise WireUnsupported("bilibili: expected a /video/BV... URL")
        view, f = await self._api("/x/web-interface/wbi/view", {"bvid": m[1]}, sign=True)
        transcript = await self._subtitles(m[1], view.get("cid"))
        comments = []
        if include_comments and view.get("aid"):
            try:
                rep, _ = await self._api(
                    "/x/v2/reply/wbi/main",
                    {"oid": view["aid"], "type": 1, "mode": 3, "ps": 20},
                    sign=True,
                )
                comments = parse_replies(rep.get("replies") or [])
            except (WireFetchError, WireUnsupported, ValueError, KeyError):
                comments = []
        stat = view.get("stat", {})
        parts = [
            view.get("desc") or "",
            f"## 字幕 Transcript\n{transcript}"
            if transcript
            else "_No CC subtitles (AI subtitles need a login)._",
        ]
        return self.doc(
            f, f"https://www.bilibili.com/video/{m[1]}", title=view.get("title"),
            author_handle=(view.get("owner") or {}).get("name"), timestamp=ts(view.get("pubdate")),
            score=stat.get("like"), body="\n\n".join(p for p in parts if p), comments=comments,
            metadata={"views": stat.get("view"), "likes": stat.get("like"), "coins": stat.get("coin"),
                      "favorites": stat.get("favorite"), "danmaku": stat.get("danmaku"),
                      "length": f"{(view.get('duration') or 0) // 60}m"},
        )  # fmt: skip

    async def _subtitles(self, bvid: str, cid) -> str:
        if not cid:
            return ""
        try:
            player, _ = await self._api("/x/player/wbi/v2", {"bvid": bvid, "cid": cid}, sign=True)
            subs = (player.get("subtitle") or {}).get("subtitles") or []
            if not subs:
                return ""
            sub = next((s for s in subs if s.get("lan", "").startswith("zh")), subs[0])
            url = sub["subtitle_url"]
            f = await self._get(url if url.startswith("http") else f"https:{url}")
            lines = parse_json(f.content).get("body", [])
            return to_paragraphs([(float(x.get("from", 0)), x.get("content", "")) for x in lines])
        except (WireFetchError, WireUnsupported, ValueError, KeyError):
            return ""

    async def search(self, query, *, limit=5, sort="relevance"):
        await self._get("https://www.bilibili.com/")  # sets buvid3, required by search
        order = {"new": "pubdate", "top": "click"}.get(sort, "totalrank")
        data, _ = await self._api("/x/web-interface/wbi/search/type",
                                  {"search_type": "video", "keyword": query, "order": order}, sign=True)  # fmt: skip
        return [WireSearchHit(url=f"https://www.bilibili.com/video/{v['bvid']}", title=_TAG.sub("", v.get("title", "")),
                              snippet=f"{v.get('author')} · {v.get('play')} plays · {v.get('description', '')[:160]}",
                              source="bilibili", rank_score=float(v.get("play") or 0))
                for v in (data.get("result") or [])[:limit]]  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().lower()
        if t in ("popular", "hot", "trending", ""):
            data, f = await self._api("/x/web-interface/popular", {"ps": limit, "pn": 1})
            videos = data.get("list", [])
        elif t.isdigit():
            data, f = await self._api(
                "/x/space/wbi/arc/search", {"mid": t, "ps": limit, "pn": 1}, sign=True
            )
            videos = (data.get("list") or {}).get("vlist", [])
        else:
            raise WireUnsupported("bilibili feeds: 'popular' or an uploader's numeric mid")
        items = [{"title": v.get("title"), "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                  "meta": [(v.get("owner") or {}).get("name") or v.get("author"),
                           f"{(v.get('stat') or {}).get('view', v.get('play'))} plays"],
                  "snippet": v.get("desc") or v.get("description") or ""} for v in videos[:limit]]  # fmt: skip
        return self.doc(f, "https://www.bilibili.com/v/popular/all", title=f"Bilibili · {t or 'popular'}",
                        body=listing(items))  # fmt: skip


CHANNEL = BilibiliChannel
