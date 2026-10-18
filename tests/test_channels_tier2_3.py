import orjson
import pytest
from conftest import FakeWaterfall, fetched

from agent_wire.channels import MODULES, channel_class
from agent_wire.channels.base import Context, WireUnsupported, assigned_json, json_ld, meta_tags
from agent_wire.channels.tier2_pro import (
    archive_bypass,
    arxiv_papers,
    fediverse,
    hubs,
    linkedin,
    markets,
    meta_social,
)
from agent_wire.channels.tier3_regional import (
    bilibili,
    boss_zhipin,
    douyin_tiktok,
    wechat,
    xiaohongshu,
    xueqiu,
    zhihu,
)
from agent_wire.engine.waterfall import WireFetchError
from agent_wire.media import audio_transcriber


class FakePool:
    """Stands in for HttpPool where channels need one shared cookie session."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append(url)
        for needle, resp in self.routes.items():
            if needle in url:
                return resp if hasattr(resp, "status") else fetched(resp, url=url)
        return fetched(b"not found", url=url, status=404)


def ctx_with(routes=None, pool_routes=None, vault=None):
    return Context(
        pool=FakePool(pool_routes or {}), waterfall=FakeWaterfall(routes or {}), vault=vault
    )


def test_every_registered_channel_loads():
    assert len(MODULES) == 31
    for name in MODULES:
        assert channel_class(name).name == name


def test_page_data_helpers():
    html = ('<meta property="og:title" content="T &amp; U"><meta name="description" content="d">'
            '<script type="application/ld+json">{"@graph": [{"@type": "Person", "name": "Ann"}]}</script>'
            '<script>window.__S__ = {"a": undefined, "b": [1, undefined]};</script>')  # fmt: skip
    assert meta_tags(html) == {"og:title": "T & U", "description": "d"}
    assert json_ld(html)[0]["name"] == "Ann"
    assert assigned_json(html, "window.__S__", js_undefined=True) == {"a": None, "b": [1, None]}
    assert assigned_json(html, "window.__MISSING__") is None


# ---- arXiv --------------------------------------------------------------------------------------

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762v7</id>
<title>Attention Is
 All You Need</title><summary> The dominant sequence transduction models... </summary>
<author><name>Ashish Vaswani</name></author><author><name>Noam Shazeer</name></author>
<published>2017-06-12T17:57:34Z</published><category term="cs.CL"/></entry></feed>"""


@pytest.mark.parametrize("url, pid", [
    ("https://arxiv.org/abs/1706.03762", "1706.03762"), ("https://arxiv.org/pdf/1706.03762v7.pdf", "1706.03762v7"),
    ("https://ar5iv.labs.arxiv.org/html/1706.03762", "1706.03762"), ("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001"),
])  # fmt: skip
def test_arxiv_ids(url, pid):
    assert arxiv_papers.arxiv_id(url) == pid


def test_arxiv_read_full_text_with_tldr(run):
    paper = (
        "<html><body><article><h1>Attention</h1><h2>Introduction</h2><p>"
        + "Transformers use attention. " * 20
        + "</p></article></body></html>"
    )
    ctx = ctx_with({"export.arxiv.org/api/query": fetched(ATOM, ctype="application/atom+xml"),
                    "semanticscholar.org": {"citationCount": 150000, "tldr": {"text": "Attention only."}},
                    "arxiv.org/html/1706.03762": fetched(paper)})  # fmt: skip
    doc = run(ctx.channel("arxiv").read("https://arxiv.org/abs/1706.03762"))
    assert doc.title == "Attention Is All You Need" and doc.metadata["citations"] == 150000
    assert (
        doc.body.startswith("TL;DR: Attention only.") and "Transformers use attention." in doc.body
    )
    assert doc.author_handle.startswith("Ashish Vaswani")


def test_arxiv_abstract_fallback(run):
    ctx = ctx_with({"export.arxiv.org/api/query": fetched(ATOM, ctype="application/atom+xml")})
    doc = run(ctx.channel("arxiv").read("https://arxiv.org/abs/1706.03762"))
    assert "## Abstract\nThe dominant sequence transduction models..." in doc.body


# ---- hubs ---------------------------------------------------------------------------------------


def test_pypi_and_npm(run):
    ctx = ctx_with({
        "pypi.org/pypi/httpx/json": {"info": {"name": "httpx", "version": "0.28.1", "summary": "HTTP client",
                                              "description": "# httpx\nFast.", "requires_python": ">=3.8",
                                              "project_urls": {"Source": "https://github.com/encode/httpx"}}},
        "registry.npmjs.org/@scope%2Fpkg": {"dist-tags": {"latest": "2.0.0"}, "description": "scoped",
                                            "readme": "# pkg", "license": "MIT", "versions": {"2.0.0": {}}},
        "api.npmjs.org/downloads": {"downloads": 1234},
    })  # fmt: skip
    doc = run(ctx.channel("hubs").read("https://pypi.org/project/httpx/"))
    assert doc.metadata["version"] == "0.28.1" and "Fast." in doc.body
    assert doc.metadata["homepage"] == "https://github.com/encode/httpx"
    doc = run(ctx.channel("hubs").read("https://www.npmjs.com/package/@scope/pkg"))
    assert doc.metadata["weekly_downloads"] == 1234 and doc.body.startswith("scoped")


def test_huggingface_model(run):
    ctx = ctx_with({"huggingface.co/api/models/meta/llama": {"downloads": 9, "likes": 3, "pipeline_tag": "text-generation",
                                                            "tags": ["license:llama3"]},
                    "huggingface.co/meta/llama/raw/main/README.md": fetched("# Llama card", ctype="text/plain")})  # fmt: skip
    doc = run(ctx.channel("hubs").read("https://huggingface.co/meta/llama"))
    assert doc.metadata["license"] == "llama3" and doc.body == "# Llama card"


def test_pypi_search_parse():
    html = '<a class="package-snippet" href="/project/httpx/"><span class="package-snippet__name">httpx</span><p class="package-snippet__description">HTTP</p></a>'
    assert hubs.parse_pypi_search(html)[0].url == "https://pypi.org/project/httpx/"


# ---- markets ------------------------------------------------------------------------------------

CHART = {"chart": {"result": [{"meta": {"symbol": "AAPL", "longName": "Apple Inc.", "regularMarketPrice": 110.0,
                                        "chartPreviousClose": 100.0, "currency": "USD", "fiftyTwoWeekHigh": 120,
                                        "fiftyTwoWeekLow": 80}}]}}  # fmt: skip


def test_market_rows_and_table():
    row = markets.yahoo_row(
        CHART, {"summaryDetail": {"trailingPE": {"raw": 31.5}, "marketCap": {"raw": 3.4e12}}}
    )
    assert row["change_pct"] == pytest.approx(10.0) and row["pe"] == 31.5
    t = markets.table([row])
    assert "| AAPL | Apple Inc. | 110.00 USD | +10.00% | 120.00 / 80.00 | 31.50 | 3400.00B |" in t
    nse = markets.nse_row({"info": {"symbol": "TCS", "companyName": "TCS"}, "priceInfo": {"lastPrice": 4000,
                           "pChange": -1.2, "weekHighLow": {"max": 4500, "min": 3500}}, "metadata": {"pdSymbolPe": 30}})  # fmt: skip
    assert nse["currency"] == "INR" and nse["pe"] == 30
    cg = markets.coingecko_row(
        {"symbol": "btc", "name": "Bitcoin", "current_price": 1, "market_cap": 2}
    )
    assert cg["symbol"] == "BTC"
    ratios = markets.screener_ratios('<ul id="top-ratios"><li><span class="name">Stock P/E</span>'
                                     '<span class="value"> 29.1 </span></li></ul>')  # fmt: skip
    assert ratios == {"Stock P/E": "29.1"}


def test_watchlist_survives_one_bad_symbol(run):
    pool = {"/v8/finance/chart/AAPL": CHART, "getcrumb": fetched("", ctype="text/plain")}
    ctx = ctx_with({}, pool)
    doc = run(ctx.channel("markets").feed("AAPL, NOPE"))
    assert "| AAPL |" in doc.body and "Unavailable: NOPE: WireFetchError" in doc.body


# ---- linkedin / meta ----------------------------------------------------------------------------


def test_linkedin_job_parse_and_id():
    assert (
        linkedin.job_id("https://www.linkedin.com/jobs/view/senior-dev-at-acme-3945123456/")
        == "3945123456"
    )
    assert (
        linkedin.job_id("https://www.linkedin.com/jobs/search/?currentJobId=3945123456")
        == "3945123456"
    )
    html = """<h2 class="top-card-layout__title">Senior Dev</h2><a class="topcard__org-name-link">Acme</a>
    <span class="topcard__flavor--bullet">Pune</span><div class="show-more-less-html__markup"><p>Build things.</p></div>
    <ul><li class="description__job-criteria-item"><h3>Seniority level</h3><span>Mid-Senior</span></li></ul>"""
    job = linkedin.parse_job(html)
    assert job == {"title": "Senior Dev", "company": "Acme", "location": "Pune", "body": "Build things.",
                   "criteria": {"Seniority level": "Mid-Senior"}}  # fmt: skip


def test_linkedin_public_post_and_login_wall():
    ld = {"@type": "SocialMediaPosting", "headline": "Shipping", "articleBody": "We shipped!",
          "author": {"name": "Ann"}, "comment": [{"text": "congrats", "author": {"name": "Bob"}}],
          "interactionStatistic": [{"interactionType": "http://schema.org/LikeAction", "userInteractionCount": 42}]}  # fmt: skip
    page = linkedin.parse_public_page(
        f'<script type="application/ld+json">{orjson.dumps(ld).decode()}</script>'
    )
    assert page["author_handle"] == "Ann" and page["comments"][0].body == "congrats"
    assert page["metadata"]["LikeAction"] == 42
    with pytest.raises(WireUnsupported, match="login wall"):
        linkedin.parse_public_page("<html><body>Sign in</body></html>")
    prof = linkedin.parse_voyager_profile({"included": [
        {"$type": "com.linkedin.voyager.identity.profile.Profile", "firstName": "Ann", "lastName": "Lee", "headline": "Eng"},
        {"$type": "com.linkedin.voyager.identity.profile.Position", "title": "CTO", "companyName": "Acme"}]})  # fmt: skip
    assert prof["title"] == "Ann Lee" and "- CTO · Acme" in prof["body"]


def test_linkedin_job_search_parse():
    html = ('<li><a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/x-1?trk=a"></a>'
            '<h3 class="base-search-card__title">Dev</h3><h4 class="base-search-card__subtitle">Acme</h4>'
            '<span class="job-search-card__location">Pune</span></li>')  # fmt: skip
    hit = linkedin.parse_job_search(html)[0]
    assert hit.url == "https://in.linkedin.com/jobs/view/x-1" and hit.snippet == "Acme · Pune"


def test_instagram_parsers():
    embed = '<div class="Caption"><a class="CaptionUsername">natgeo</a> A whale breaching.</div>'
    assert meta_social.parse_ig_embed(embed)["body"] == "A whale breaching."
    og = '<meta property="og:description" content="1,234 likes, 56 comments - natgeo on June 1, 2025: &quot;Whales!&quot;">'
    post = meta_social.parse_og_social(og)
    assert post["body"] == "Whales!" and post["metadata"] == {"likes": 1234, "comments": 56}
    prof = meta_social.parse_ig_profile({"data": {"user": {"username": "natgeo", "biography": "bio",
        "edge_followed_by": {"count": 9}, "edge_owner_to_timeline_media": {"count": 1, "edges": [
            {"node": {"shortcode": "abc", "edge_liked_by": {"count": 5},
                      "edge_media_to_caption": {"edges": [{"node": {"text": "cap"}}]}}}]}}}})  # fmt: skip
    assert prof["metadata"]["followers"] == 9 and "/p/abc/ · ♥5 · cap" in prof["body"]


# ---- publishing / fediverse / archive -----------------------------------------------------------


def test_substack_post_with_comments(run):
    ctx = ctx_with({
        "/api/v1/posts/hello": {"id": 7, "title": "Hello", "subtitle": "Sub", "body_html": "<p>Body text</p>",
                                "post_date": "2025-01-01T00:00:00Z", "comment_count": 1, "audience": "everyone",
                                "publishedBylines": [{"name": "Writer"}]},
        "/api/v1/post/7/comments": {"comments": [{"id": 1, "body": "great", "name": "R", "reaction_count": 2,
                                                  "children": [{"id": 2, "body": "agree", "name": "S"}]}]},
    })  # fmt: skip
    doc = run(ctx.channel("publishing").read("https://writer.substack.com/p/hello"))
    assert doc.body == "Sub\n\nBody text" and doc.author_handle == "Writer"
    assert doc.comments[0].replies[0].body == "agree"


def test_publishing_feeds_map_to_rss(run):
    rss = fetched(b"<rss><channel><title>N</title></channel></rss>", ctype="application/rss+xml")
    ctx = ctx_with({"/feed": rss})
    run(ctx.channel("publishing").feed("platformer"))
    run(ctx.channel("publishing").feed("@karpathy"))
    urls = [u for u, _ in ctx.waterfall.calls]
    assert urls == ["https://platformer.substack.com/feed", "https://medium.com/feed/@karpathy"]


def test_bluesky_post(run):
    thread = {"thread": {"post": {"uri": "at://did:plc:x/app.bsky.feed.post/3k", "cid": "c0",
                                  "author": {"handle": "jay.bsky.team"}, "likeCount": 10,
                                  "record": {"text": "hello sky", "createdAt": "2024-02-06T00:00:00Z"}},
                         "replies": [{"post": {"cid": "c1", "author": {"handle": "jay.bsky.team"}, "likeCount": 1,
                                               "record": {"text": "self reply"}}, "replies": []}]}}  # fmt: skip
    ctx = ctx_with({"resolveHandle": {"did": "did:plc:x"}, "getPostThread": thread})
    doc = run(ctx.channel("fediverse").read("https://bsky.app/profile/jay.bsky.team/post/3k"))
    assert doc.body == "hello sky" and doc.comments[0].is_op
    assert (
        "did%3Aplc%3Ax/app.bsky.feed.post/3k" in ctx.waterfall.calls[1][0]
    )  # handle resolved to DID


def test_mastodon_status_and_context(run):
    ctx = ctx_with({
        "/statuses/1/context": {"descendants": [
            {"id": "2", "in_reply_to_id": "1", "content": "<p>reply</p>", "account": {"acct": "b"}},
            {"id": "3", "in_reply_to_id": "2", "content": "<p>nested</p>", "account": {"acct": "a"}}]},
        "/api/v1/statuses/1": {"content": "<p>Toot <b>toot</b></p>", "account": {"acct": "a"}, "favourites_count": 4},
    })  # fmt: skip
    doc = run(ctx.channel("fediverse").read("https://mastodon.social/@a/1"))
    assert doc.body == "Toot toot" and doc.comments[0].replies[0].is_op


def test_archive_helpers():
    assert archive_bypass.raw_wayback("https://web.archive.org/web/20200101000000/https://ex.com/a?b=1") == \
        "https://web.archive.org/web/20200101000000id_/https://ex.com/a?b=1"  # fmt: skip
    rows = [["timestamp", "original", "statuscode"], ["2020", "https://ex.com", "200"]]
    assert archive_bypass.parse_cdx(rows) == [
        {"timestamp": "2020", "original": "https://ex.com", "statuscode": "200"}
    ]
    assert fediverse.parse_mastodon_context([], "1", "a") == []


# ---- bilibili -----------------------------------------------------------------------------------


def test_wbi_signature_matches_reference_vector():
    key = bilibili.mixin_key("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45")
    assert key == "ea1db124af3c7062474693fa704f4ff8"
    signed = bilibili.wbi_sign({"foo": "114", "bar": "514", "zab": 1919810}, key, now=1702204169)
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"


def test_bilibili_read(run):
    nav = {
        "code": -101,
        "data": {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }
        },
    }
    view = {
        "code": 0,
        "data": {
            "title": "测试视频",
            "desc": "简介",
            "owner": {"name": "UP主"},
            "aid": 1,
            "cid": 2,
            "stat": {"view": 100, "like": 5},
            "duration": 125,
            "pubdate": 1700000000,
        },
    }
    player = {
        "code": 0,
        "data": {
            "subtitle": {
                "subtitles": [{"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/s.json"}]
            }
        },
    }
    subs = {
        "body": [
            {"from": 0.0, "to": 1.0, "content": "大家好"},
            {"from": 1.0, "to": 2.0, "content": "今天测试"},
        ]
    }
    reply = {"code": 0, "data": {"replies": [{"rpid": 9, "content": {"message": "好看"}, "member": {"uname": "fan"},
                                              "like": 3, "replies": []}]}}  # fmt: skip
    ctx = ctx_with({}, {"/x/web-interface/nav": nav, "/x/web-interface/wbi/view": view, "/x/player/wbi/v2": player,
                        "aisubtitle.hdslb.com": subs, "/x/v2/reply/wbi/main": reply})  # fmt: skip
    doc = run(ctx.channel("bilibili").read("https://www.bilibili.com/video/BV1GJ411x7h7/"))
    assert (
        doc.title == "测试视频" and doc.metadata["views"] == 100 and doc.metadata["length"] == "2m"
    )
    assert "大家好 今天测试" in doc.body and doc.comments[0].body == "好看"
    signed_call = next(u for u in ctx.pool.calls if "/x/player/wbi/v2" in u)
    assert "w_rid=" in signed_call and "wts=" in signed_call


def test_bilibili_needs_bvid(run):
    with pytest.raises(WireUnsupported):
        run(ctx_with().channel("bilibili").read("https://www.bilibili.com/anime/"))


# ---- xiaohongshu / wechat / zhihu / v2ex --------------------------------------------------------


def test_xiaohongshu_state():
    nid = "65a1b2c3d4e5f6a7b8c9d0e1"
    html = ('<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"' + nid + '":{"note":{"title":"咖啡",'
            '"desc":"很好喝","user":{"nickname":"小红"},"interactInfo":{"likedCount":"1.2万","commentCount":"30"},'
            '"tagList":[{"name":"探店"}],"imageList":[{},{}],"time":1700000000000,"extra":undefined},'
            '"comments":{"list":[{"id":"c","content":"想去","userInfo":{"nickname":"a"},"likeCount":"5"}]}}}}}</script>')  # fmt: skip
    note = xiaohongshu.parse_note(
        assigned_json(html, "window.__INITIAL_STATE__", js_undefined=True), nid
    )
    assert note["body"] == "很好喝\n\n#探店" and note["metadata"]["likes"] == 12000
    assert note["comments"][0].body == "想去" and note["metadata"]["images"] == 2
    with pytest.raises(WireUnsupported):
        xiaohongshu.parse_note({}, nid)


def test_wechat_hidden_body_is_kept():
    html = (
        '<h1 id="activity-name"> 标题 </h1><a id="js_name">公众号</a><script>var ct = "1700000000";</script>'
        '<div id="js_content" style="visibility: hidden;"><p>正文第一段</p><p style="display:none">注入</p></div>'
    )
    art = wechat.parse_article(html)
    assert (
        art["title"] == "标题"
        and art["author_handle"] == "公众号"
        and art["timestamp"].year == 2023
    )
    assert art["body"] == "正文第一段"  # wrapper un-hidden, real hidden node still stripped
    hits = wechat.parse_sogou('<ul class="news-list"><li><h3><a href="/link?url=x">文章</a></h3>'
                              '<p class="txt-info">摘要</p></li></ul>')  # fmt: skip
    assert hits[0].url == "https://weixin.sogou.com/link?url=x"


def test_zhihu_question_and_article():
    state = {"initialState": {"entities": {
        "questions": {"1": {"title": "Q?", "detail": "<p>details</p>", "answerCount": 2}},
        "answers": {"10": {"id": 10, "content": "<p>A1</p>", "voteupCount": 50, "author": {"name": "x"}},
                    "11": {"id": 11, "content": "<p>A2</p>", "voteupCount": 5, "author": {"name": "y"}}},
        "articles": {"9": {"title": "Art", "content": "<p>body</p>", "author": {"name": "z"}}}}}}  # fmt: skip
    q = zhihu.parse_initial(state, "question", "1")
    assert q["title"] == "Q?" and [c.score for c in q["comments"]] == [50, 5]
    assert [c.id for c in zhihu.parse_initial(state, "question", "1", "11")["comments"]] == ["11"]
    assert zhihu.parse_initial(state, "article", "9")["body"] == "body"


def test_v2ex_topic(run):
    ctx = ctx_with({"topics/show.json?id=1": [{"title": "T", "content": "c", "member": {"username": "op"},
                                               "replies": 1, "node": {"title": "Python"}}],
                    "replies/show.json?topic_id=1": [{"id": 5, "content": "r", "member": {"username": "op"}}]})  # fmt: skip
    doc = run(ctx.channel("v2ex").read("https://www.v2ex.com/t/1#reply1"))
    assert doc.metadata["node"] == "Python" and doc.comments[0].is_op


# ---- tiktok / douyin / xiaoyuzhou / xueqiu / boss ------------------------------------------------


def test_tiktok_and_douyin_parsers():
    tk = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": {
        "desc": "cat video", "author": {"uniqueId": "catlover"}, "createTime": 1700000000,
        "stats": {"playCount": 10, "diggCount": 2}, "video": {"subtitleInfos": [{"Url": "u", "LanguageCodeName": "eng-US"}]}}}}}}  # fmt: skip
    info = douyin_tiktok.parse_tiktok(tk)
    assert info["author_handle"] == "catlover" and info["_subs"][0]["Url"] == "u"
    dy = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [
        {"desc": "抖音", "author": {"nickname": "作者"}, "statistics": {"digg_count": 7}}]}}}}  # fmt: skip
    assert douyin_tiktok.parse_douyin(dy)["metadata"]["likes"] == 7
    with pytest.raises(WireUnsupported):
        douyin_tiktok.parse_tiktok({})


def test_xiaoyuzhou_episode(run, monkeypatch):
    monkeypatch.delenv("WIRE_TRANSCRIBE", raising=False)
    nd = {"props": {"pageProps": {"episode": {"title": "第1期", "shownotes": "<p>本期聊</p>", "duration": 3600,
                                              "enclosure": {"url": "https://media.xyzcdn.net/a.m4a"},
                                              "podcast": {"title": "播客"}}}}}  # fmt: skip
    page = (
        f'<script id="__NEXT_DATA__" type="application/json">{orjson.dumps(nd).decode()}</script>'
    )
    ctx = ctx_with({"xiaoyuzhoufm.com/episode": fetched(page)})
    doc = run(ctx.channel("xiaoyuzhou").read("https://www.xiaoyuzhoufm.com/episode/abc"))
    assert (
        doc.body == "本期聊"
        and doc.metadata["audio"].endswith("a.m4a")
        and doc.metadata["length"] == "60m"
    )


def test_xueqiu_parsers():
    q = xueqiu.parse_quote({"data": {"quote": {"name": "贵州茅台", "symbol": "SH600519", "current": 1500.5,
                                               "percent": 1.2, "currency": "CNY", "pe_ttm": 25}}})  # fmt: skip
    assert q["title"] == "贵州茅台 (SH600519)" and "+1.2%" in q["body"]
    s = xueqiu.parse_statuses(
        {"list": [{"id": 1, "text": "<p>看好</p>", "user": {"screen_name": "u"}, "like_count": 3}]}
    )
    assert s[0].body == "看好" and s[0].score == 3


def test_boss_zhipin():
    job = boss_zhipin.parse_job('<div class="name"><h1>Python工程师</h1></div><span class="salary">20-30K</span>'
                                '<div class="job-sec-text">负责后端</div>')  # fmt: skip
    assert job["title"] == "Python工程师" and job["metadata"]["salary"] == "20-30K"
    with pytest.raises(WireUnsupported, match="verification"):
        boss_zhipin.parse_job("<html>请完成验证</html>")


def test_transcriber_off_by_default(run, monkeypatch):
    monkeypatch.delenv("WIRE_TRANSCRIBE_API_KEY", raising=False)
    monkeypatch.setattr(audio_transcriber.importlib.util, "find_spec", lambda name: None)
    assert audio_transcriber.backend() is None
    with pytest.raises(audio_transcriber.TranscriptionUnavailable):
        run(audio_transcriber.transcribe(FakePool({}), "https://x/a.mp3"))


def test_nse_falls_back_to_yahoo(run):
    chart = {"chart": {"result": [{"meta": {"symbol": "TCS.NS", "regularMarketPrice": 2350.0,
                                            "chartPreviousClose": 2300.0, "currency": "INR"}}]}}  # fmt: skip
    ctx = ctx_with(
        {}, {"nseindia.com/api": fetched(b"denied", status=403), "/v8/finance/chart/TCS.NS": chart}
    )
    row, _ = run(ctx.channel("markets").quote("nse:tcs"))
    assert row["symbol"] == "TCS.NS" and row["currency"] == "INR"


def test_arxiv_search_falls_back_to_semantic_scholar(run):
    ctx = ctx_with({"api.semanticscholar.org/graph/v1/paper/search": {"data": [
        {"paperId": "p1", "title": "MoE", "externalIds": {"ArXiv": "2101.03961"}, "year": 2021, "citationCount": 9},
        {"paperId": "p2", "title": "Other", "externalIds": {}}]}})  # fmt: skip
    hits = run(ctx.channel("arxiv").search("mixture of experts"))
    assert [h.url for h in hits] == [
        "https://arxiv.org/abs/2101.03961",
        "https://www.semanticscholar.org/paper/p2",
    ]


def test_mastodon_boosts_show_original_content(run):
    ctx = ctx_with({"accounts/lookup": {"id": "1"}, "accounts/1/statuses": [
        {"url": "https://m.s/u/1/activity", "content": "", "reblog": {"url": "https://o/@x/2", "content": "<p>boosted</p>"}}]})  # fmt: skip
    doc = run(ctx.channel("fediverse").feed("@Gargron@mastodon.social"))
    assert "1. [boosted](https://o/@x/2)" in doc.body


def test_fetch_errors_propagate_as_wire_errors(run):
    with pytest.raises(WireFetchError):
        run(ctx_with().channel("v2ex").read("https://www.v2ex.com/t/404"))
