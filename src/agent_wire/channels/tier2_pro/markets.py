"""Markets: Yahoo Finance (global), NSE India, Screener.in ratios, CoinGecko crypto."""

import asyncio
from urllib.parse import parse_qs, quote, urlsplit

from curl_cffi.requests.exceptions import RequestException
from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, ts
from agent_wire.engine.http_pool import Fetched
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireSearchHit

YF = "https://query1.finance.yahoo.com"
CG = "https://api.coingecko.com/api/v3"
ERRORS = (WireFetchError, RequestException, ValueError, KeyError, IndexError, TypeError)


def _num(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, int | float) and abs(v) >= 1e9:
        return f"{v / 1e9:.{digits}f}B"
    if isinstance(v, int | float) and abs(v) >= 1e6:
        return f"{v / 1e6:.{digits}f}M"
    return f"{v:,.{digits}f}" if isinstance(v, int | float) else str(v)


def yahoo_row(chart: dict, summary: dict | None = None) -> dict:
    meta = chart["chart"]["result"][0]["meta"]
    price, prev = (
        meta.get("regularMarketPrice"),
        meta.get("chartPreviousClose") or meta.get("previousClose"),
    )
    detail = (summary or {}).get("summaryDetail", {})
    raw = lambda k: (
        (detail.get(k) or {}).get("raw") if isinstance(detail.get(k), dict) else detail.get(k)
    )
    return {
        "symbol": meta.get("symbol"), "name": meta.get("longName") or meta.get("shortName"),
        "price": price, "currency": meta.get("currency"),
        "change_pct": (price - prev) / prev * 100 if price and prev else None,
        "high52": meta.get("fiftyTwoWeekHigh"), "low52": meta.get("fiftyTwoWeekLow"),
        "pe": raw("trailingPE"), "mcap": raw("marketCap"), "exchange": meta.get("fullExchangeName"),
    }  # fmt: skip


def nse_row(data: dict) -> dict:
    info, price = data.get("info", {}), data.get("priceInfo", {})
    hl = price.get("weekHighLow", {})
    return {"symbol": info.get("symbol"), "name": info.get("companyName"), "price": price.get("lastPrice"),
            "currency": "INR", "change_pct": price.get("pChange"), "high52": hl.get("max"), "low52": hl.get("min"),
            "pe": data.get("metadata", {}).get("pdSymbolPe"), "mcap": None, "exchange": "NSE"}  # fmt: skip


def coingecko_row(m: dict) -> dict:
    return {"symbol": (m.get("symbol") or "").upper(), "name": m.get("name"), "price": m.get("current_price"),
            "currency": "USD", "change_pct": m.get("price_change_percentage_24h"), "high52": m.get("ath"),
            "low52": m.get("atl"), "pe": None, "mcap": m.get("market_cap"), "exchange": "CoinGecko (ATH/ATL)"}  # fmt: skip


def screener_ratios(html: str) -> dict[str, str]:
    out = {}
    for li in LexborHTMLParser(html).css("#top-ratios li"):
        name, value = li.css_first(".name"), li.css_first(".value")
        if name and value:
            out[name.text(strip=True)] = " ".join(value.text(strip=True).split())
    return out


def table(rows: list[dict]) -> str:
    lines = [
        "| Symbol | Name | Price | Chg % | 52W H / L | P/E | Mkt cap |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        chg = f"{r['change_pct']:+.2f}%" if isinstance(r.get("change_pct"), int | float) else "—"
        lines.append(f"| {r['symbol']} | {r.get('name') or ''} | {_num(r['price'])} {r.get('currency') or ''} | {chg} "
                     f"| {_num(r.get('high52'))} / {_num(r.get('low52'))} | {_num(r.get('pe'))} | {_num(r.get('mcap'))} |")  # fmt: skip
    return "\n".join(lines)


class MarketsChannel(Channel):
    name = "markets"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._crumb: str | None = None

    async def _chrome(self, url: str, **kw) -> Fetched:
        """Anonymous shared chrome session: Yahoo/NSE need the cookies they set on a first visit."""
        return await self.ctx.pool.get(url, impersonate="chrome", **kw)

    async def _yahoo(self, sym: str) -> tuple[dict, Fetched]:
        f = await self._chrome(f"{YF}/v8/finance/chart/{quote(sym)}?range=5d&interval=1d")
        if not f.ok:
            raise WireFetchError(f"yahoo {sym}: HTTP {f.status}")
        chart = parse_json(f.content)
        summary = None
        try:
            if not self._crumb:
                await self._chrome("https://fc.yahoo.com/")
                self._crumb = (await self._chrome(f"{YF}/v1/test/getcrumb")).text.strip() or None
            if self._crumb:
                s = await self._chrome(f"{YF}/v10/finance/quoteSummary/{quote(sym)}?modules=summaryDetail"
                                       f"&crumb={quote(self._crumb)}")  # fmt: skip
                summary = parse_json(s.content)["quoteSummary"]["result"][0] if s.ok else None
        except ERRORS:
            summary = None  # P/E and market cap are a bonus over the chart quote
        return yahoo_row(chart, summary), f

    async def _nse(self, sym: str) -> tuple[dict, Fetched]:
        await self._chrome("https://www.nseindia.com/")
        f = await self._chrome(f"https://www.nseindia.com/api/quote-equity?symbol={quote(sym)}",
                               headers={"Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"})  # fmt: skip
        if not f.ok:
            raise WireFetchError(f"nse {sym}: HTTP {f.status}")
        return nse_row(parse_json(f.content)), f

    async def _coin(self, ident: str) -> tuple[dict, Fetched]:
        coin_id = ident.lower()
        found, _ = await self.fetch_json(f"{CG}/search?query={quote(coin_id)}")
        if found.get("coins"):
            coin_id = found["coins"][0]["id"]
        data, f = await self.fetch_json(f"{CG}/coins/markets?vs_currency=usd&ids={quote(coin_id)}")
        if not data:
            raise WireUnsupported(f"no CoinGecko coin for {ident!r}")
        return coingecko_row(data[0]), f

    async def quote(self, token: str) -> tuple[dict, Fetched]:
        t = token.strip()
        prefix, _, rest = t.partition(":")
        if rest and prefix.lower() in ("crypto", "cg", "coin"):
            return await self._coin(rest)
        if rest and prefix.lower() == "nse":
            try:
                return await self._nse(rest.upper())
            except ERRORS:  # NSE's Akamai front blocks many networks; Yahoo carries NSE listings
                return await self._yahoo(f"{rest.upper()}.NS")
        return await self._yahoo(t.upper())

    async def _news(self, sym: str) -> str:
        try:
            data, _ = await self.fetch_json(
                f"{YF}/v1/finance/search?q={quote(sym)}&newsCount=5&quotesCount=0"
            )
        except ERRORS:
            return ""
        return "\n".join(f"- [{n['title']}]({n['link']}) · {n.get('publisher', '')} · "
                         f"{ts(n.get('providerPublishTime')).date() if n.get('providerPublishTime') else ''}"
                         for n in data.get("news", []))  # fmt: skip

    async def read(self, url, *, query=None, section=None, include_comments=True):
        p = urlsplit(url)
        host, segs = (p.hostname or "").removeprefix("www."), [s for s in p.path.split("/") if s]
        if host == "screener.in" and segs[:1] == ["company"] and len(segs) > 1:
            f = await self.fetch(url, archive=False)
            ratios = screener_ratios(f.text)
            body = "\n".join(f"- {k}: {v}" for k, v in ratios.items())
            return self.doc(
                f,
                url,
                title=f"{segs[1]} · Screener.in",
                body=body,
                metadata={"source": "screener.in"},
            )
        if host == "nseindia.com":
            token = "nse:" + parse_qs(p.query).get("symbol", [""])[0]
        elif host == "coingecko.com":
            token = "crypto:" + segs[-1]
        else:  # finance.yahoo.com/quote/SYM
            token = segs[segs.index("quote") + 1] if "quote" in segs else segs[-1]
        row, f = await self.quote(token)
        news = await self._news(row["symbol"]) if host == "finance.yahoo.com" else ""
        body = table([row]) + (f"\n\n## News\n{news}" if news else "")
        return self.doc(f, url, title=f"{row.get('name') or row['symbol']} quote", body=body,
                        metadata={"exchange": row.get("exchange")})  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        tokens = [t for t in target.replace(";", ",").split(",") if t.strip()][:limit]
        results = await asyncio.gather(*(self.quote(t) for t in tokens), return_exceptions=True)
        rows, errors, last = [], [], None
        for tok, r in zip(tokens, results, strict=True):
            if isinstance(r, BaseException):
                if not isinstance(r, ERRORS + (WireUnsupported,)):
                    raise r
                errors.append(f"{tok.strip()}: {type(r).__name__}")
            else:
                rows.append(r[0])
                last = r[1]
        if not rows:
            raise WireUnsupported("no quotes resolved: " + "; ".join(errors))
        body = table(rows) + (f"\n\nUnavailable: {'; '.join(errors)}" if errors else "")
        return self.doc(last, "https://finance.yahoo.com/", title="Watchlist", body=body)

    async def search(self, query, *, limit=5, sort="relevance"):
        data, _ = await self.fetch_json(
            f"{YF}/v1/finance/search?q={quote(query)}&quotesCount={limit}&newsCount=0"
        )
        return [WireSearchHit(url=f"https://finance.yahoo.com/quote/{q['symbol']}",
                              title=f"{q['symbol']} · {q.get('shortname') or q.get('longname') or ''}",
                              snippet=f"{q.get('quoteType', '')} on {q.get('exchDisp', '')}", source="yahoo")
                for q in data.get("quotes", [])[:limit] if q.get("symbol")]  # fmt: skip


CHANNEL = MarketsChannel
