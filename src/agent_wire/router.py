"""Sub-millisecond URL -> channel and platform-name -> channel routing."""

import re
from urllib.parse import urlsplit

from agent_wire.channels import MODULES

DOMAINS = {
    # tier 1
    "reddit.com": "reddit", "redd.it": "reddit",
    "x.com": "x", "twitter.com": "x", "fxtwitter.com": "x", "vxtwitter.com": "x", "fixupx.com": "x",
    "github.com": "github", "githubusercontent.com": "github",
    "youtube.com": "youtube", "youtu.be": "youtube", "youtube-nocookie.com": "youtube",
    "news.ycombinator.com": "hackernews", "lobste.rs": "hackernews",
    "stackoverflow.com": "stackoverflow", "stackexchange.com": "stackoverflow",
    "superuser.com": "stackoverflow", "serverfault.com": "stackoverflow",
    "askubuntu.com": "stackoverflow", "mathoverflow.net": "stackoverflow",
    "wikipedia.org": "wikipedia",
    # tier 2
    "arxiv.org": "arxiv", "ar5iv.org": "arxiv",
    "linkedin.com": "linkedin",
    "pypi.org": "hubs", "npmjs.com": "hubs", "crates.io": "hubs", "huggingface.co": "hubs",
    "finance.yahoo.com": "markets", "nseindia.com": "markets", "screener.in": "markets",
    "coingecko.com": "markets",
    "instagram.com": "meta_social", "facebook.com": "meta_social", "fb.watch": "meta_social",
    "medium.com": "publishing", "substack.com": "publishing",
    "bsky.app": "fediverse",
    "polymarket.com": "polymarket", "stocktwits.com": "stocktwits", "dev.to": "devto",
    "web.archive.org": "archive", "archive.ph": "archive", "archive.today": "archive",
    "archive.is": "archive",
    # tier 3
    "bilibili.com": "bilibili", "b23.tv": "bilibili",
    "xiaohongshu.com": "xiaohongshu", "xhslink.com": "xiaohongshu",
    "mp.weixin.qq.com": "wechat",
    "zhihu.com": "zhihu",
    "v2ex.com": "v2ex",
    "douyin.com": "douyin_tiktok", "iesdouyin.com": "douyin_tiktok", "tiktok.com": "douyin_tiktok",
    "xiaoyuzhoufm.com": "xiaoyuzhou",
    "xueqiu.com": "xueqiu",
    "zhipin.com": "boss_zhipin",
}  # fmt: skip

PLATFORMS = {
    "web": "search", "search": "search",
    "reddit": "reddit",
    "x": "x", "twitter": "x",
    "github": "github", "gh": "github",
    "youtube": "youtube", "yt": "youtube",
    "hn": "hackernews", "hackernews": "hackernews", "lobsters": "hackernews",
    "stackoverflow": "stackoverflow", "so": "stackoverflow", "stackexchange": "stackoverflow",
    "wikipedia": "wikipedia", "wiki": "wikipedia",
    "rss": "rss", "atom": "rss",
    "arxiv": "arxiv", "papers": "arxiv", "scholar": "arxiv",
    "linkedin": "linkedin", "jobs": "linkedin",
    "hubs": "hubs", "pypi": "hubs", "npm": "hubs", "crates": "hubs", "huggingface": "hubs",
    "hf": "hubs",
    "markets": "markets", "stocks": "markets", "crypto": "markets", "finance": "markets",
    "instagram": "meta_social", "facebook": "meta_social", "meta": "meta_social",
    "medium": "publishing", "substack": "publishing",
    "bluesky": "fediverse", "bsky": "fediverse", "mastodon": "fediverse", "fediverse": "fediverse",
    "archive": "archive", "wayback": "archive",
    "polymarket": "polymarket", "predictions": "polymarket", "odds": "polymarket",
    "news": "news", "techmeme": "news", "googlenews": "news",
    "stocktwits": "stocktwits", "devto": "devto", "dev.to": "devto",
    "bilibili": "bilibili", "xhs": "xiaohongshu", "xiaohongshu": "xiaohongshu", "red": "xiaohongshu",
    "wechat": "wechat", "weixin": "wechat", "zhihu": "zhihu", "v2ex": "v2ex",
    "douyin": "douyin_tiktok", "tiktok": "douyin_tiktok", "xiaoyuzhou": "xiaoyuzhou",
    "xueqiu": "xueqiu", "boss": "boss_zhipin", "zhipin": "boss_zhipin",
}  # fmt: skip

# Every channel's own name is also a valid platform name.
PLATFORMS.update({name: name for name in MODULES if name != "web"})

# `platforms=["all"]` fans out to the channels with fast, key-free search.
ALL_PLATFORMS = ("web", "reddit", "hn", "github", "youtube", "stackoverflow", "wikipedia")

_MASTODON_STATUS = re.compile(r"^/@[\w.]+(@[\w.-]+)?(/\d+)?/?$")  # status or profile
_FEED_PATH = re.compile(r"(\.rss|\.xml|\.atom|/feed/?|/rss/?|/atom/?)$", re.IGNORECASE)


def route_url(url: str) -> str:
    parts = urlsplit(url if "//" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    for i in range(len(labels) - 1):
        if channel := DOMAINS.get(".".join(labels[i:])):
            return channel
    if _MASTODON_STATUS.match(parts.path):
        return "fediverse"
    if host.endswith(".substack.com") or _is_substack_post(parts.path):
        return "publishing"
    if _FEED_PATH.search(parts.path):
        return "rss"
    return "web"


def _is_substack_post(path: str) -> bool:
    return False  # custom-domain Substacks are detected by the web channel from page markup


def route_platform(platform: str) -> str:
    key = platform.strip().lower()
    if key not in PLATFORMS:
        raise ValueError(
            f"unknown platform {platform!r}; try one of: {', '.join(sorted(PLATFORMS))}"
        )
    return PLATFORMS[key]
