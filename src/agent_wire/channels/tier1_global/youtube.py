"""YouTube: InnerTube metadata + in-memory transcript, chapters, top comments, search, channel feeds."""

import re
from urllib.parse import parse_qs, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, assigned_json, compact_count, listing
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.media.subtitle_cleaner import parse_captions, parse_chapters, to_paragraphs
from agent_wire.models import WireComment, WireSearchHit

INNERTUBE = "https://www.youtube.com/youtubei/v1"
ANDROID = {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 30,
           "hl": "en", "gl": "US"}  # fmt: skip
ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"
WEB = {"clientName": "WEB", "clientVersion": "2.20250925.01.00", "hl": "en", "gl": "US"}
MAX_COMMENTS = 40
_ID = re.compile(r"^[\w-]{11}$")


def video_id(url: str) -> str | None:
    p = urlsplit(url)
    host, segs = (p.hostname or ""), [s for s in p.path.split("/") if s]
    if host.endswith("youtu.be") and segs:
        cand = segs[0]
    elif segs[:1] in (["shorts"], ["embed"], ["live"], ["v"]) and len(segs) > 1:
        cand = segs[1]
    else:
        cand = parse_qs(p.query).get("v", [""])[0]
    return cand if _ID.match(cand or "") else None


def pick_track(tracks: list[dict], lang: str = "en") -> dict | None:
    def rank(t):
        manual = t.get("kind") != "asr"
        same = t.get("languageCode", "").startswith(lang)
        return (manual and same, same, manual)

    return max(tracks, key=rank) if tracks else None


def find_all(obj, key: str):
    """Yield every value stored under `key` anywhere in a nested InnerTube response."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_all(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_all(v, key)


def _runs(obj) -> str:
    if not obj:
        return ""
    return obj.get("simpleText") or "".join(r.get("text", "") for r in obj.get("runs", []))


def parse_search(data: dict, limit: int) -> list[WireSearchHit]:
    hits = []
    for v in find_all(data, "videoRenderer"):
        if not v.get("videoId"):
            continue
        hits.append(
            WireSearchHit(
                url=f"https://www.youtube.com/watch?v={v['videoId']}", title=_runs(v.get("title")),
                snippet=" · ".join(x for x in (_runs(v.get("ownerText")), _runs(v.get("viewCountText")),
                                               _runs(v.get("lengthText")), _runs(v.get("publishedTimeText"))) if x),
                source="youtube", rank_score=float(compact_count(_runs(v.get("viewCountText")))),
            )
        )  # fmt: skip
        if len(hits) >= limit:
            break
    return hits


def parse_lockups(data: dict, limit: int) -> list[dict]:
    """Channel Videos tab (2025+ `lockupViewModel` layout) -> listing items."""
    items = []
    for lv in find_all(data, "lockupViewModel"):
        meta = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        parts = [p.get("text", {}).get("content") for rows in find_all(meta, "metadataRows")
                 for row in rows for p in row.get("metadataParts") or []]  # fmt: skip
        if lv.get("contentId"):
            items.append({"title": (meta.get("title") or {}).get("content"),
                          "url": f"https://www.youtube.com/watch?v={lv['contentId']}", "meta": parts})  # fmt: skip
        if len(items) >= limit:
            break
    return items


def parse_comment_mutations(data: dict) -> list[WireComment]:
    out = []
    for p in find_all(data, "commentEntityPayload"):
        props, author = p.get("properties", {}), p.get("author", {})
        toolbar = p.get("toolbar", {})
        out.append(
            WireComment(
                id=props.get("commentId", str(len(out))), body=props.get("content", {}).get("content", ""),
                author_handle=(author.get("displayName") or "").lstrip("@") or None,
                score=compact_count(toolbar.get("likeCountNotliked") or toolbar.get("likeCountA11y")),
                is_op=bool(author.get("isCreator")),
            )
        )  # fmt: skip
    return out[:MAX_COMMENTS]


class YouTubeChannel(Channel):
    name = "youtube"

    async def _innertube(self, endpoint: str, payload: dict, client: dict = WEB):
        headers = {"Content-Type": "application/json"}
        if client is ANDROID:
            headers["User-Agent"] = ANDROID_UA
        data, f = await self.fetch_json(
            f"{INNERTUBE}/{endpoint}?prettyPrint=false", method="POST",
            json_body={"context": {"client": client}, **payload}, headers=headers,
        )  # fmt: skip
        return data, f

    async def read(self, url, *, query=None, section=None, include_comments=True):
        vid = video_id(url)
        if not vid:
            return await self.feed(url)
        player, f = await self._innertube("player", {"videoId": vid}, ANDROID)
        details = player.get("videoDetails") or {}
        if not details:
            reason = (player.get("playabilityStatus") or {}).get("reason", "unavailable")
            raise WireUnsupported(f"youtube {vid}: {reason}")
        desc = details.get("shortDescription", "")
        chapters = parse_chapters(desc)
        transcript = await self._transcript(player, chapters)
        parts = [desc.strip()[:1500]]
        parts.append(f"## Transcript\n{transcript}" if transcript else "_No captions available._")
        comments = await self._comments(vid) if include_comments else []
        micro = (player.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        return self.doc(
            f, f"https://www.youtube.com/watch?v={vid}", title=details.get("title"),
            author_handle=details.get("author"), body="\n\n".join(p for p in parts if p),
            comments=comments,
            metadata={"views": int(details.get("viewCount") or 0),
                      "length": f"{int(details.get('lengthSeconds') or 0) // 60}m",
                      "uploaded": micro.get("uploadDate") or micro.get("publishDate"),
                      "chapters": len(chapters) or None},
        )  # fmt: skip

    async def _transcript(self, player: dict, chapters) -> str:
        tracks = (
            (player.get("captions") or {})
            .get("playerCaptionsTracklistRenderer", {})
            .get("captionTracks", [])
        )
        track = pick_track(tracks)
        if not track:
            return ""
        base = re.sub(r"&fmt=[^&]*", "", track["baseUrl"])
        try:
            f = await self.fetch(f"{base}&fmt=json3", archive=False, browser=False)
            segs = parse_captions(f.content) if f.content.strip() else []
        except (WireFetchError, ValueError):
            return ""
        return to_paragraphs(segs, chapters)

    async def _comments(self, vid: str) -> list[WireComment]:
        try:
            nxt, _ = await self._innertube("next", {"videoId": vid})
            token = next(
                (c["continuationCommand"]["token"]
                 for sec in find_all(nxt, "itemSectionRenderer")
                 if sec.get("sectionIdentifier") == "comment-item-section"
                 for c in find_all(sec, "continuationEndpoint") if "continuationCommand" in c),
                None,
            )  # fmt: skip
            if not token:
                return []
            page, _ = await self._innertube("next", {"continuation": token})
            return parse_comment_mutations(page)
        except (WireFetchError, ValueError, KeyError):
            return []  # comments are a bonus; the transcript is the payload

    async def search(self, query, *, limit=5, sort="relevance"):
        data, _ = await self._innertube("search", {"query": query})
        return parse_search(data, limit)

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip()
        channel_id = t if re.match(r"^UC[\w-]{22}$", t) else None
        if not channel_id:
            page_url = (
                t
                if t.startswith("http")
                else f"https://www.youtube.com/{t if t.startswith('@') else '@' + t}"
            )
            f = await self.fetch(page_url, archive=False)
            # canonical / externalId name the page's own channel; a bare "channelId" can be a recommendation
            m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', f.text)
                 or re.search(r'"externalId":"(UC[\w-]{22})"', f.text))  # fmt: skip
            if not m:
                raise WireUnsupported(f"couldn't resolve YouTube channel for {target!r}")
            channel_id = m[1]
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            return await self.ctx.channel("rss").feed(rss, limit=limit)
        except WireFetchError:  # the RSS endpoint 500s intermittently; read the Videos tab instead
            url = f"https://www.youtube.com/channel/{channel_id}/videos"
            f = await self.fetch(url, archive=False)
            data = assigned_json(f.text, "var ytInitialData") or {}
            name = next(find_all(data, "channelMetadataRenderer"), {}).get("title") or channel_id
            return self.doc(
                f, url, title=f"{name} · videos", body=listing(parse_lockups(data, limit))
            )


CHANNEL = YouTubeChannel
