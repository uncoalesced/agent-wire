"""Lazy channel registry: modules import on first use so `wire serve` starts fast."""

import importlib

MODULES = {
    "web": "tier1_global.web",
    "search": "tier1_global.search",
    "github": "tier1_global.github",
    "reddit": "tier1_global.reddit",
    "x": "tier1_global.x_twitter",
    "youtube": "tier1_global.youtube",
    "hackernews": "tier1_global.hackernews",
    "stackoverflow": "tier1_global.stackoverflow",
    "wikipedia": "tier1_global.wikipedia",
    "rss": "tier1_global.rss",
    "arxiv": "tier2_pro.arxiv_papers",
    "linkedin": "tier2_pro.linkedin",
    "hubs": "tier2_pro.hubs",
    "markets": "tier2_pro.markets",
    "meta_social": "tier2_pro.meta_social",
    "publishing": "tier2_pro.publishing",
    "fediverse": "tier2_pro.fediverse",
    "archive": "tier2_pro.archive_bypass",
    "polymarket": "tier2_pro.polymarket",
    "news": "tier2_pro.news",
    "stocktwits": "tier2_pro.stocktwits",
    "devto": "tier2_pro.devto",
    "bilibili": "tier3_regional.bilibili",
    "xiaohongshu": "tier3_regional.xiaohongshu",
    "wechat": "tier3_regional.wechat",
    "zhihu": "tier3_regional.zhihu",
    "v2ex": "tier3_regional.v2ex",
    "douyin_tiktok": "tier3_regional.douyin_tiktok",
    "xiaoyuzhou": "tier3_regional.xiaoyuzhou",
    "xueqiu": "tier3_regional.xueqiu",
    "boss_zhipin": "tier3_regional.boss_zhipin",
}


def channel_class(name: str):
    return importlib.import_module(f"agent_wire.channels.{MODULES[name]}").CHANNEL
