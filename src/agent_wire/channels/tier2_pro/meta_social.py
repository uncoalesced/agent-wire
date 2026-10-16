"""Instagram and Facebook: public embeds / web-profile API / OpenGraph, cookie escalation if logged in."""

import re
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, compact_count, json_ld, meta_tags, ts
from agent_wire.engine.waterfall import WireFetchError

IG_APP_ID = "936619743392459"  # public id the Instagram web app sends with every request
_IG_POST = re.compile(r"/(?:p|reel|reels|tv)/([\w-]+)")


def parse_ig_embed(html: str) -> dict:
    tree = LexborHTMLParser(html)
    cap = tree.css_first(".Caption")
    user = tree.css_first(".CaptionUsername, .UsernameText")
    text = cap.text(separator=" ", strip=True) if cap else ""
    name = user.text(strip=True) if user else None
    if name and text.startswith(name):
        text = text[len(name) :].strip()
    likes = tree.css_first(".SocialProof a, .likes")
    return {"author_handle": name, "body": text,
            "metadata": {"likes": compact_count(likes.text()) if likes else None}}  # fmt: skip


def parse_og_social(html: str) -> dict:
    """og:description like '1,234 likes, 56 comments - user on June 1, 2025: "caption"'."""
    m = meta_tags(html)
    desc = m.get("og:description") or m.get("description") or ""
    if not desc and not m.get("og:title"):
        raise WireUnsupported("login wall; run `wire auth login instagram.com` (or facebook.com)")
    likes = re.search(r"([\d.,KM]+)\s+likes?", desc)
    comments = re.search(r"([\d.,KM]+)\s+comments?", desc)
    caption = re.search(r':\s*"(.*)"\s*\.?$', desc, re.DOTALL)
    return {"title": m.get("og:title"), "body": caption[1] if caption else desc,
            "metadata": {"likes": compact_count(likes[1]) if likes else None,
                         "comments": compact_count(comments[1]) if comments else None}}  # fmt: skip


def parse_ig_profile(data: dict) -> dict:
    u = data.get("data", {}).get("user") or {}
    posts = [e["node"] for e in (u.get("edge_owner_to_timeline_media") or {}).get("edges", [])]
    lines = [u.get("biography") or ""]
    for p in posts[:12]:
        caps = (p.get("edge_media_to_caption") or {}).get("edges") or [{}]
        cap = (caps[0].get("node") or {}).get("text", "")
        likes = (p.get("edge_liked_by") or p.get("edge_media_preview_like") or {}).get("count")
        lines.append(
            f"- https://www.instagram.com/p/{p.get('shortcode')}/ · ♥{likes} · {cap[:160]}"
        )
    return {"title": f"{u.get('full_name') or u.get('username')} (@{u.get('username')})",
            "author_handle": u.get("username"), "body": "\n".join(x for x in lines if x),
            "metadata": {"followers": (u.get("edge_followed_by") or {}).get("count"),
                         "following": (u.get("edge_follow") or {}).get("count"),
                         "posts": (u.get("edge_owner_to_timeline_media") or {}).get("count")}}  # fmt: skip


class MetaSocialChannel(Channel):
    name = "meta_social"

    async def _ig(self, url: str, **kw):
        # Fetch as the "instagram" channel so the outbound firewall applies its login-only rules.
        return await self.fetch(url, channel="instagram", archive=False, **kw)

    async def read(self, url, *, query=None, section=None, include_comments=True):
        p = urlsplit(url)
        host = (p.hostname or "").removeprefix("www.").removeprefix("m.")
        if host == "instagram.com":
            if m := _IG_POST.search(p.path):
                code = m[1]
                try:
                    f = await self._ig(f"https://www.instagram.com/p/{code}/embed/captioned/")
                    post = parse_ig_embed(f.text)
                    if post["body"]:
                        return self.doc(
                            f, url, title=f"@{post['author_handle']} on Instagram", **post
                        )
                except WireFetchError:
                    pass
                f = await self._ig(url)
                return self.doc(f, url, **parse_og_social(f.text))
            user = p.path.strip("/").split("/")[0]
            if user:
                try:
                    data, f = await self.fetch_json(
                        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={user}",
                        headers={"x-ig-app-id": IG_APP_ID}, channel="instagram",
                    )  # fmt: skip
                    return self.doc(f, url, **parse_ig_profile(data))
                except (WireFetchError, ValueError):
                    f = await self._ig(url)
                    return self.doc(f, url, **parse_og_social(f.text))
        f = await self.fetch(url, archive=False)  # facebook public pages / posts / watch
        for item in json_ld(f.text):
            if item.get("articleBody") or item.get("description"):
                return self.doc(f, url, title=item.get("headline") or item.get("name"),
                                body=item.get("articleBody") or item.get("description"),
                                timestamp=ts(item.get("datePublished")))  # fmt: skip
        return self.doc(f, url, **parse_og_social(f.text))


CHANNEL = MetaSocialChannel
