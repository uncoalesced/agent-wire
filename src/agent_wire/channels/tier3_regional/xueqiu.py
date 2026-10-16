"""Xueqiu (雪球): stock quotes and hot discussions, after priming the anonymous xq_a_token cookie."""

import re
from urllib.parse import quote, urlsplit

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, ts
from agent_wire.engine.http_pool import Fetched
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireComment


def parse_quote(data: dict) -> dict:
    q = (data.get("data") or {}).get("quote") or {}
    if not q:
        raise WireUnsupported("xueqiu returned no quote (unknown symbol?)")
    lines = [f"Price: {q.get('current')} {q.get('currency') or ''} ({q.get('percent'):+}%)" if q.get("percent") is not None
             else f"Price: {q.get('current')}",
             f"52W: {q.get('high52w')} / {q.get('low52w')} · P/E (TTM): {q.get('pe_ttm')} · Market cap: {q.get('market_capital')}"]  # fmt: skip
    return {"title": f"{q.get('name')} ({q.get('symbol')})", "body": "\n".join(lines),
            "metadata": {"exchange": q.get("exchange")}}  # fmt: skip


def parse_statuses(data: dict) -> list[WireComment]:
    return [WireComment(id=str(s.get("id")), body=html_to_markdown(s.get("text") or s.get("description") or ""),
                        author_handle=(s.get("user") or {}).get("screen_name"), score=s.get("like_count") or 0,
                        timestamp=ts(s.get("created_at")))
            for s in data.get("list", [])]  # fmt: skip


class XueqiuChannel(Channel):
    name = "xueqiu"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._primed = False

    async def _get(self, url: str) -> Fetched:
        if not self._primed:  # anonymous visitor cookie, kept in the shared chrome session
            await self.ctx.pool.get("https://xueqiu.com/", impersonate="chrome")
            self._primed = True
        f = await self.ctx.pool.get(
            url, impersonate="chrome", headers={"Referer": "https://xueqiu.com/"}
        )
        if not f.ok:
            raise WireFetchError(
                f"xueqiu: HTTP {f.status}; its WAF needs a JS challenge: install the stealth extra "
                "or run `wire auth login xueqiu.com`"
            )
        return f

    async def _stock(self, url: str, symbol: str, include_comments: bool):
        f = await self._get(
            f"https://stock.xueqiu.com/v5/stock/quote.json?symbol={quote(symbol)}&extend=detail"
        )
        info = parse_quote(parse_json(f.content))
        if include_comments:
            try:
                s = await self._get("https://xueqiu.com/query/v1/symbol/search/status.json?count=15&comment=0"
                                    f"&symbol={quote(symbol)}&hl=0&source=all&sort=alpha&page=1")  # fmt: skip
                info["comments"] = parse_statuses(parse_json(s.content))
            except (WireFetchError, ValueError):
                pass
        return self.doc(f, url, **info)

    async def read(self, url, *, query=None, section=None, include_comments=True):
        m = re.search(r"/S/([\w.]+)", urlsplit(url).path)
        if not m:
            return await self.ctx.channel("web").read(url, query=query, section=section)
        return await self._stock(url, m[1].upper(), include_comments)

    async def feed(self, target, *, sort="hot", limit=10):
        sym = target.strip().upper()
        return await self._stock(f"https://xueqiu.com/S/{sym}", sym, True)


CHANNEL = XueqiuChannel
