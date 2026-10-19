from datetime import UTC, datetime, timedelta

import pytest
from conftest import fetched
from typer.testing import CliRunner

from agent_wire.channels.base import WireUnsupported
from agent_wire.channels.tier1_global import hackernews, reddit, rss, web
from agent_wire.channels.tier2_pro import fediverse, news, polymarket, stocktwits
from agent_wire.compression.dam_formatter import format_comments
from agent_wire.engine.challenge import is_blocked
from agent_wire.engine.http_pool import Fetched
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.router import route_platform, route_url

NOW = datetime.now(UTC)


def test_new_routes():
    assert route_url("https://polymarket.com/event/fed") == "polymarket"
    assert route_url("https://stocktwits.com/symbol/NVDA") == "stocktwits"
    assert route_url("https://dev.to/u/post") == "devto"
    assert route_platform("techmeme") == "news" and route_platform("odds") == "polymarket"


# ---- polymarket ---------------------------------------------------------------------------------
EVENT = {"slug": "fed-oct", "title": "Fed decision in October?", "volume": "4500000.5",
         "volume24hr": 385081.7, "liquidity": 600000, "commentCount": 3, "closed": False,
         "endDate": (NOW + timedelta(days=10)).isoformat(),
         "markets": [{"groupItemTitle": "25 bps cut", "outcomes": '["Yes", "No"]',
                      "outcomePrices": '["0.62", "0.38"]', "volume": "300",
                      "oneWeekPriceChange": 0.05},
                     {"groupItemTitle": "No change", "outcomes": '["Yes", "No"]',
                      "outcomePrices": '["0.3", "0.7"]', "volume": "100"}]}  # fmt: skip


def test_polymarket_odds_search_and_read(make_ctx, run):
    h = polymarket.event_hit(EVENT)
    assert h.url == "https://polymarket.com/event/fed-oct" and h.engagement["volume24h"] == 385081
    assert h.snippet.startswith("25 bps cut: Yes 62% · No 38% (+5 pts)")
    old = dict(EVENT, slug="old", endDate=(NOW - timedelta(days=90)).isoformat())
    ctx = make_ctx({"public-search": {"events": [EVENT, old, dict(EVENT, closed=True)]},
                    "events?slug": [EVENT]})  # fmt: skip
    hits = run(ctx.channel("polymarket").search("fed", since=NOW - timedelta(days=30)))
    assert [x.title for x in hits] == ["Fed decision in October?"]
    doc = run(ctx.channel("polymarket").read("https://polymarket.com/event/fed-oct"))
    assert "Markets:" in doc.body and doc.metadata["volume"] == 4500000


# ---- stocktwits ---------------------------------------------------------------------------------
STREAM = {"symbol": {"title": "NVIDIA", "watchlist_count": 1000},
          "messages": [{"id": 1, "body": "$NVDA up", "created_at": NOW.isoformat(),
                        "user": {"username": "a"}, "entities": {"sentiment": {"basic": "Bullish"}},
                        "likes": {"total": 4}},
                       {"id": 2, "body": "$NVDA down", "created_at": NOW.isoformat(),
                        "user": {"username": "b"}, "entities": {"sentiment": None}}]}  # fmt: skip


def test_stocktwits(make_ctx, run):
    assert stocktwits.symbol_of("$nvda") == "NVDA" and stocktwits.symbol_of("BTC.X") == "BTC.X"
    with pytest.raises(WireUnsupported):
        stocktwits.symbol_of("what is nvidia doing")
    assert stocktwits.sentiment(STREAM) == "1 bullish / 0 bearish"
    ctx = make_ctx({"streams/symbol/NVDA": STREAM})
    doc = run(ctx.channel("stocktwits").feed("$NVDA"))
    assert "NVIDIA · 1,000 watchers · 1 bullish" in doc.body and "@a · Bullish" in doc.body


# ---- dev.to / news ------------------------------------------------------------------------------


def test_devto_search(make_ctx, run):
    art = {"title": "Rust tips", "url": "https://dev.to/u/rust-tips", "published_at": NOW.isoformat(),
           "public_reactions_count": 50, "comments_count": 7, "user": {"username": "u"}}  # fmt: skip
    ctx = make_ctx({"tag=rpt&": [], "tag=rustprogrammingtips&": [], "dev.to/api/articles": [art]})
    hits = run(ctx.channel("devto").search("rust programming tips"))
    assert hits[0].engagement == {"likes": 50, "comments": 7}
    assert "tag=programming" in ctx.waterfall.calls[-1][0]  # empty guesses -> longest topic word


GN = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Big launch - The Outlet</title>
<link>https://news.google.com/rss/articles/abc</link><pubDate>Fri, 09 Oct 2026 14:05:37 GMT</pubDate>
<source url="https://outlet.example">The Outlet</source></item></channel></rss>"""


def test_google_news_and_techmeme(make_ctx, run):
    h = news.gnews_hits(news.parse_rss_items(GN.encode()))[0]
    assert h.title == "Big launch" and h.author == "The Outlet" and h.timestamp.year == 2026
    desc = '<a href="https://techmeme.com/r">r</a><a href="https://bloomberg.com/x">b</a>'
    tm = news.techmeme_hits([{"title": "X ships Y (Bloomberg)", "url": "https://techmeme.com/1",
                              "date": NOW, "desc": desc}])[0]  # fmt: skip
    assert tm.title == "X ships Y" and tm.link == "https://bloomberg.com/x"
    assert news.matches(tm, "x ships") and not news.matches(tm, "x ships zebra")
    ctx = make_ctx({"news.google.com": fetched(GN, ctype="application/xml"),
                    "techmeme.com": fetched("<html>down</html>")})  # fmt: skip
    hits = run(ctx.channel("news").search("big launch", since=NOW - timedelta(days=7)))
    assert hits[0].title == "Big launch"  # a broken Techmeme feed doesn't sink the search
    assert "when%3A7d" in ctx.waterfall.calls[0][0]
    with pytest.raises(WireUnsupported):
        news.parse_rss_items(b"<html><body>nope")


class FakePool:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        return next(fetched(body, url=url) for k, body in self.routes.items() if k in url)


def test_google_news_links_decode_to_the_publisher(make_ctx, run):
    target = "https://outlet.example/story"
    batch = ')]}\'\n\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"' + target + '\\",1]",null]]'
    assert news.parse_batch(batch) == target and news.parse_batch("junk") is None
    page = b'<c-wiz><div data-n-a-sg="SIG" data-n-a-ts="1792418090"></div>'
    ctx = make_ctx({"news.google.com": fetched(GN, ctype="application/xml"),
                    "techmeme.com": fetched("<html>down</html>")})  # fmt: skip
    ctx.pool = FakePool({"batchexecute": batch, "rss/articles/abc": page})
    hits = run(ctx.channel("news").search("big launch"))
    assert hits[0].url == target and hits[0].author == "The Outlet"
    form = next(kw for u, kw in ctx.pool.calls if "batchexecute" in u)["data"]["f.req"]
    assert '\\"abc\\", 1792418090, \\"SIG\\"' in form
    ctx.pool = FakePool({"rss/articles/abc": b"<html>no signature</html>"})
    assert run(ctx.channel("news").decode("https://news.google.com/rss/articles/abc")).endswith(
        "/abc"
    )


# ---- reddit via Arctic Shift --------------------------------------------------------------------
POST = {"id": "abc", "title": "uv is great", "score": 12, "num_comments": 1,
        "created_utc": int((NOW - timedelta(days=2)).timestamp()),
        "permalink": "/r/Python/comments/abc/uv_is_great/", "is_self": True, "selftext": "body",
        "url": "https://www.reddit.com/r/Python/comments/abc/", "author": "op",
        "subreddit_name_prefixed": "r/Python"}  # fmt: skip
TREE = [{"kind": "t1", "data": {"id": "c1", "body": "agreed", "author": "x", "score": 3,
                                "created_utc": 1791044600, "replies": ""}}]  # fmt: skip
ARCTIC = "arctic-shift.photon-reddit.com/api"


def arctic_routes(extra=None):
    routes = {
        f"{ARCTIC}/posts/ids": {"data": [POST]},
        f"{ARCTIC}/comments/tree": {"data": TREE},
        f"{ARCTIC}/subreddits/search": {
            "data": [
                {"display_name": "Python", "subscribers": 1000},
                {"display_name": "pythontips", "subscribers": 10},
            ]
        },
        f"{ARCTIC}/posts/search": {"data": [POST]},
    }
    routes.update(extra or {})
    routes["reddit.com"] = fetched(b"<html>blocked</html>", status=403)  # matched last
    return routes


def test_reddit_falls_back_to_arctic_shift(make_ctx, run):
    ctx = make_ctx(arctic_routes())
    doc = run(
        ctx.channel("reddit").read("https://www.reddit.com/r/Python/comments/abc/uv_is_great/")
    )
    assert doc.title == "uv is great" and doc.comments[0].body == "agreed"
    assert "Arctic Shift" in doc.metadata["source"]
    hits = run(ctx.channel("reddit").search("uv python", since=NOW - timedelta(days=30)))
    assert hits and hits[0].engagement["archived"] == 1 and hits[0].source == "reddit"
    feed = run(ctx.channel("reddit").feed("r/Python"))
    assert "Arctic Shift" in feed.title and "uv is great" in feed.body
    assert reddit.post_hit(dict(POST, selftext="[removed]")).snippet == ""


def test_reddit_arctic_retries_once_then_explains(make_ctx, run, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(reddit.asyncio, "sleep", no_sleep)
    busy = {f"{ARCTIC}/posts/ids": {"data": None, "error": "Timeout. Maybe slow down a bit"}}
    ctx = make_ctx(arctic_routes(busy))
    with pytest.raises(WireFetchError, match="slow down"):
        run(ctx.channel("reddit").read("https://www.reddit.com/r/Python/comments/abc/x/"))
    assert sum("posts/ids" in u for u, _ in ctx.waterfall.calls) == 2


RSS = f"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t5_sub</id><title>python</title><link href="https://www.reddit.com/r/python/"/></entry>
<entry><author><name>/u/op</name></author><category term="Python"/><id>t3_abc</id>
<content type="html">&lt;div class="md"&gt;&lt;p&gt;uv body&lt;/p&gt;&lt;/div&gt; submitted by
&lt;a href="https://www.reddit.com/user/op"&gt;/u/op&lt;/a&gt;
&lt;span&gt;&lt;a href="https://astral.sh/uv"&gt;[link]&lt;/a&gt;&lt;/span&gt;</content>
<link href="https://www.reddit.com/r/Python/comments/abc/uv_is_great/"/>
<published>{NOW.isoformat()}</published><title>uv is great</title></entry>
<entry><id>t3_old</id><title>old</title><link href="https://www.reddit.com/r/Python/comments/old/"/>
<published>{(NOW - timedelta(days=90)).isoformat()}</published></entry></feed>""".encode()


def test_reddit_search_uses_live_rss_when_json_is_blocked(make_ctx, run):
    routes = {
        "search.rss": fetched(RSS),
        "reddit.com": fetched(b"<html>blocked</html>", status=403),
    }
    ctx = make_ctx(routes)
    hits = run(ctx.channel("reddit").search("uv python", since=NOW - timedelta(days=30)))
    assert [h.title for h in hits] == ["uv is great"]  # subreddit result and out-of-window dropped
    h = hits[0]
    assert h.author == "op" and h.link == "https://astral.sh/uv" and h.snippet == "uv body"
    assert "t=month" in next(u for u, _ in ctx.waterfall.calls if "search.rss" in u)
    assert reddit.rss_window(None) == "all" and reddit.rss_window(NOW - timedelta(days=3)) == "week"


def test_reddit_search_falls_back_to_pullpush(make_ctx, run):
    routes = {"pullpush.io": {"data": [POST]}, "search.rss": fetched(b"", status=429)}
    routes["reddit.com"] = fetched(b"<html>blocked</html>", status=403)
    ctx = make_ctx(routes)
    hits = run(ctx.channel("reddit").search("uv python", since=NOW - timedelta(days=30)))
    assert hits[0].title == "uv is great" and hits[0].engagement["score"] == 12
    assert not any("arctic-shift" in u for u, _ in ctx.waterfall.calls)


# ---- fediverse ----------------------------------------------------------------------------------


def test_bluesky_hits_and_mastodon_tag(make_ctx, run):
    post = {"uri": "at://did/app.bsky.feed.post/3k", "author": {"handle": "a.bsky.social"},
            "record": {"text": "hi", "createdAt": NOW.isoformat()}, "likeCount": 5,
            "repostCount": 1}  # fmt: skip
    toot = {"url": "https://m.s/@u/1", "content": "<p>py</p>", "created_at": NOW.isoformat(),
            "account": {"acct": "u"}, "favourites_count": 2}  # fmt: skip
    ctx = make_ctx({"searchPosts": {"posts": [post]}, "timelines/tag/python": [toot]})
    hits = run(ctx.channel("fediverse").search("x", since=NOW - timedelta(days=1)))
    assert hits[0].engagement["likes"] == 5 and "since=" in ctx.waterfall.calls[0][0]
    assert ctx.waterfall.calls[0][0].startswith("https://api.bsky.app/")  # public.api 403s search
    m = run(ctx.channel("fediverse").mastodon_tag("#python!"))
    assert m[0].source == "mastodon" and m[0].snippet == "py"
    assert fediverse.BSKY == "https://api.bsky.app/xrpc"


def test_mastodon_profile_url_reads_account_feed_else_web(make_ctx, run):
    toot = {
        "url": "https://m.s/@u/1",
        "content": "<p>hello fedi</p>",
        "created_at": NOW.isoformat(),
    }
    ctx = make_ctx({"accounts/lookup?acct=u": {"id": "9"}, "accounts/9/statuses": [toot]})
    doc = run(ctx.channel("fediverse").read("https://m.s/@u"))
    assert doc.title == "@u@m.s" and "hello fedi" in doc.body
    page = fetched(b"<html><title>Team</title><body><p>We are the team.</p></body></html>",
                   url="https://example.org/@team", ctype="text/html")  # fmt: skip
    ctx = make_ctx({"api/v1/accounts": fetched(b"<html>nope</html>"), "example.org": page})
    doc = run(ctx.channel("fediverse").read("https://example.org/@team"))
    assert "the team" in doc.body  # not a Mastodon host: plain web read


# ---- github person mode -------------------------------------------------------------------------


def test_github_person_and_windowed_search(make_ctx, run, monkeypatch):
    from agent_wire.channels.tier1_global import github

    monkeypatch.setattr(github, "github_token", lambda: None)
    pr = {"title": "Fix bug", "html_url": "https://github.com/a/b/pull/1", "state": "closed",
          "created_at": NOW.isoformat(), "pull_request": {"merged_at": NOW.isoformat()}}  # fmt: skip
    issue = {"html_url": "https://github.com/a/b/issues/2", "title": "Crash", "comments": 4,
             "repository_url": "https://api.github.com/repos/a/b", "created_at": NOW.isoformat(),
             "user": {"login": "z"}}  # fmt: skip
    ctx = make_ctx({
        "search/issues?q=author:octo": {"total_count": 2, "items": [pr]},
        "users/octo/repos": [{"full_name": "octo/x", "html_url": "https://github.com/octo/x",
                              "stargazers_count": 9, "pushed_at": NOW.isoformat()}],
        "search/repositories": {"items": []},
        "search/issues": {"items": [issue]},
    })  # fmt: skip
    doc = run(ctx.channel("github").person("@octo", since=NOW - timedelta(days=30)))
    assert "PRs opened in window: 2 · merged (sampled): 1" in doc.body and "octo/x" in doc.body
    hits = run(ctx.channel("github").search("crash", since=NOW - timedelta(days=30)))
    assert hits[0].title == "a/b: Crash" and hits[0].engagement["comments"] == 4


# ---- parking-lot fixes --------------------------------------------------------------------------


def test_hn_comment_score_reads_as_replies():
    tree = [{"id": 1, "author": "a", "text": "t",
             "children": [{"id": 2, "author": "b", "text": "r", "children": []}]}]  # fmt: skip
    cs = hackernews.parse_hn_comments(tree, op=None)
    assert cs[0].score_kind == "replies" and "[1 replies]" in format_comments(cs)


def test_rss_url_serving_html_is_read_as_a_page(make_ctx, run):
    page = ("<html><head><title>Blog</title></head><body><article><h1>Hello</h1>"
            + "<p>words here and there</p>" * 30 + "</article></body></html>")  # fmt: skip
    ctx = make_ctx({"example.com/feed": fetched(page)})
    doc = run(ctx.channel("rss").read("https://example.com/feed"))
    assert doc.channel == "web" and "words here" in doc.body
    with pytest.raises(rss.NotAFeed):
        rss.parse_feed(b"<html><body>x</body></html>")


def test_web_drops_crawl_day_dates():
    today = NOW.date().isoformat()
    assert web._real_date(today, "<p>no dates</p>") is None
    assert web._real_date(today, '<meta property="article:published_time" content="x">') == today
    assert web._real_date("2020-01-02", "<p>") == "2020-01-02"


def test_rendered_pages_are_not_js_shells():
    tiny = Fetched(
        "https://a/", 200, {"content-type": "text/html"}, b"<html><body>OK</body></html>"
    )
    assert is_blocked(tiny) and not is_blocked(tiny, rendered=True)
    cf = Fetched("https://a/", 200, {"content-type": "text/html"},
                 b"<html><title>Just a moment...</title></html>")  # fmt: skip
    assert is_blocked(cf, rendered=True)


def test_install_auto_asks_first(tmp_path, monkeypatch):
    from agent_wire import cli

    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("APPDATA", raising=False)
    (tmp_path / ".cursor").mkdir()
    r = CliRunner().invoke(cli.app, ["install", "--project", str(tmp_path)], input="n\n")
    assert r.exit_code == 1 and "detected clients: cursor" in r.output
    assert "nothing written" in r.output and not (tmp_path / ".cursor" / "mcp.json").exists()
    r = CliRunner().invoke(cli.app, ["install", "--project", str(tmp_path), "--yes"])
    assert r.exit_code == 0 and (tmp_path / ".cursor" / "mcp.json").exists()


def test_devto_tries_acronym_then_joined_then_longest_word(make_ctx, run):
    from agent_wire.channels.tier2_pro import devto

    assert devto.tag_candidates("Model Context Protocol") == [
        "mcp",
        "modelcontextprotocol",
        "protocol",
    ]
    assert devto.tag_candidates("OpenAI vs Anthropic") == ["openaianthropic", "anthropic"]
    mcp = {"title": "Building an MCP server", "description": "tools", "url": "https://dev.to/a/1"}
    ctx = make_ctx({"tag=mcp&": [mcp]})
    hits = run(ctx.channel("devto").search("Model Context Protocol"))
    assert hits[0].title == "Building an MCP server" and len(ctx.waterfall.calls) == 1
    off = {"title": "Zip archives", "description": "compression", "url": "https://dev.to/a/2"}
    ctx = make_ctx({"tag=rar&": [off], "tag=rustasyncruntime&": [], "tag=runtime&": [mcp]})
    hits = run(ctx.channel("devto").search("rust async runtime"))
    assert hits[0].url == "https://dev.to/a/1"  # off-topic acronym tag skipped


def test_devto_tags_and_arxiv_html_search():
    from agent_wire.channels.tier2_pro import arxiv_papers, devto

    assert devto._tags({"tag_list": "a, b", "tags": ["a", "b"]}) == "a, b"
    assert devto._tags({"tag_list": ["a", "b"]}) == "a, b" and devto._tags({}) is None
    html = (
        '<ol><li class="arxiv-result"><p class="list-title"><a href="https://arxiv.org/abs/1">x</a></p>'
        '<p class="title">Diffusion <span>LMs</span></p><p class="authors"><a>Ann</a><a>Bo</a></p>'
        '<span class="abstract-full">We study things. △ Less</span>'
        '<p class="is-size-7">Submitted 15 October, 2026; originally announced</p></li></ol>'
    )
    h = arxiv_papers.parse_search_html(html)[0]
    assert h.title == "Diffusion LMs" and h.author == "Ann, Bo" and h.snippet == "We study things."
    assert h.timestamp == datetime(2026, 10, 15, tzinfo=UTC)


def test_arxiv_search_falls_back_to_openalex(make_ctx, run, monkeypatch):
    from agent_wire.channels.tier2_pro import arxiv_papers

    work = {"title": "Masked Diffusion LMs", "publication_date": "2026-07-23", "cited_by_count": 46,
            "authorships": [{"author": {"display_name": "Ann"}}],
            "locations": [{"landing_page_url": "https://doi.org/x"},
                          {"landing_page_url": "http://arxiv.org/abs/2406.07524"}],
            "abstract_inverted_index": {"We": [0], "diffuse.": [2], "text": [1]}}  # fmt: skip
    ctx = make_ctx(
        {"export.arxiv.org": fetched(b"", status=429), "api.openalex.org": {"results": [work]}}
    )
    hits = run(ctx.channel("arxiv").search("diffusion language models"))
    h = hits[0]
    assert h.url == "https://arxiv.org/abs/2406.07524" and h.snippet == "We text diffuse."
    assert h.timestamp == datetime(2026, 7, 23, tzinfo=UTC) and h.engagement == {"citations": 46}
    assert "S4306400194" in next(u for u, _ in ctx.waterfall.calls if "openalex" in u)
    assert arxiv_papers.openalex_hit({"locations": []}) is None
