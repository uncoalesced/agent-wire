import asyncio

from conftest import FakeWaterfall, fetched
from fastmcp import Client

from agent_wire.channels.base import Context
from agent_wire.research import brief as briefmod
from agent_wire.research.engine import Researcher
from agent_wire.server import Wire, create_server
from agent_wire.verify import citations
from agent_wire.verify.citations import Checker, Citation

ARTICLE = (
    "<html><head><title>Rust 2.0 released</title></head><body><article><h1>Rust 2.0 released</h1>"
    + "<p>The Rust team shipped version 2.0 today with a new borrow checker and faster builds.</p>"
    * 5
    + "</article></body></html>"
)
NOT_FOUND = "<html><head><title>Page not found</title></head><body><p>Sorry.</p></body></html>"
WAYBACK = {"archived_snapshots": {"closest": {"available": True, "timestamp": "20240101000000"}}}


def run(coro):
    return asyncio.run(coro)


def page(html, **kw):
    """Serve `html` from whatever URL was requested (no redirect)."""
    return lambda url, _kw: fetched(html, url=url, **kw)


def gone(status):
    from agent_wire.engine.waterfall import WireFetchError

    def raise_(url, kw):
        raise WireFetchError(f"{url}: all waterfall stages failed (HTTP {status})")

    return raise_


class FakePool:
    """Wayback availability API + CDX index. `archived` decides whether a capture exists;
    `limited` makes the availability API answer 429 like it does under load."""

    def __init__(self, archived=True, limited=False, cdx_down=False):
        self.archived, self.limited, self.cdx_down = archived, limited, cdx_down
        self.calls = []

    async def get(self, url, params=None, impersonate=None, **kw):
        self.calls.append((url, params))
        if "cdx" in url:
            if self.cdx_down:
                return fetched("<h1>429</h1>", url=url, status=429)
            rows = [["timestamp", "original"]]
            if self.archived:
                rows.append(["20000815064912", params["url"]])
            return fetched(rows, url=url)
        if self.limited:
            return fetched("<h1>429 Too Many Requests</h1>", url=url, status=429)
        return fetched(WAYBACK if self.archived else {"archived_snapshots": {}}, url=url)


def check(routes, url, quote=None, pool=None, check_quotes=True):
    waterfall = FakeWaterfall(routes)
    result = run(Checker(waterfall, pool).check(Citation(url, quote), check_quotes=check_quotes))
    return result, waterfall


# ---- parsing -------------------------------------------------------------------------------------


def test_canary_claims_are_the_sentence_before_the_marker():
    text = (
        "Rust is fast. Ferris is the mascot [[UNSOURCED]].\n"
        "- Go was released in 2009. [[unsourced]]\n"
        "No marker here."
    )
    assert citations.find_canaries(text) == ["Ferris is the mascot", "Go was released in 2009."]
    assert citations.find_canaries("[[UNSOURCED]]") == ["(marker with no claim text on its line)"]
    assert citations.find_canaries("Penguin Random House published it.") == []


def test_extract_markdown_and_bare_links_dedup_and_quotes():
    text = (
        'Per [the post](https://blog.example.com/a/b), "shipped version 2.0 today with a new" '
        "borrow checker.\n"
        "See https://en.wikipedia.org/wiki/Rust_(programming_language).\n"
        "Again: https://www.blog.example.com/a/b/#top?utm_source=x and `code`."
    )
    cites = citations.extract_citations(text)
    assert [c.url for c in cites] == [
        "https://blog.example.com/a/b",
        "https://en.wikipedia.org/wiki/Rust_(programming_language)",
    ]
    assert cites[0].quote == "shipped version 2.0 today with a new"
    assert cites[1].quote is None


def test_normalize_url_ignores_noise_but_keeps_real_query():
    n = citations.normalize_url
    assert n("http://WWW.Example.com/x/?utm_source=t#frag") == n("https://example.com/x")
    assert n("https://example.com/x?id=1") != n("https://example.com/x?id=2")


def test_quote_found_exact_fuzzy_and_missing():
    body = "The Rust team shipped version 2.0 today, with a new borrow checker and faster builds."
    assert citations.quote_found("shipped version 2.0 today with a new borrow checker", body)
    assert citations.quote_found("the rust team shipped version 2 today with a new checker", body)
    assert not citations.quote_found("Rust was abandoned by its maintainers last year", body)


def test_search_hint_uses_title_or_slug():
    assert citations.search_hint("https://www.example.com/2024/rust-2-released.html") == (
        "rust released example.com"
    )
    assert citations.search_hint("https://x.dev/p/9", "Rust 2.0") == "Rust 2.0 x.dev"


# ---- probes --------------------------------------------------------------------------------------


def test_live_page_is_ok_with_title_and_excerpt_and_no_archive_fallback():
    c, wf = check({"example.com": page(ARTICLE)}, "https://example.com/rust")
    assert c.verdict == "OK" and c.title == "Rust 2.0 released" and "borrow checker" in c.excerpt
    assert wf.calls[0][1] == {"archive": False, "browser": False}


def test_dead_link_with_archive_copy_is_archived():
    pool = FakePool(archived=True)
    c, _ = check({"example.com": gone(404)}, "https://example.com/old", pool=pool)
    assert c.verdict == "ARCHIVED" and c.status == 404
    assert c.archive == "https://web.archive.org/web/20240101000000/https://example.com/old"
    assert pool.calls[0][1] == {"url": "https://example.com/old"}


def test_rate_limited_availability_api_falls_back_to_cdx():
    pool = FakePool(archived=True, limited=True)
    c, _ = check({"example.com": gone(404)}, "https://example.com/old", pool=pool)
    assert c.verdict == "ARCHIVED"
    assert c.archive == "https://web.archive.org/web/20000815064912/https://example.com/old"
    c, _ = check({"example.com": gone(404)}, "https://example.com/old",
                 pool=FakePool(limited=True, cdx_down=True))  # fmt: skip
    assert c.verdict == "DEAD" and "archive lookup failed" in c.note


def test_dead_link_without_archive_is_dead_with_replacement_hint():
    c, _ = check({"example.com": gone(410)}, "https://example.com/old-post", pool=FakePool(False))
    assert c.verdict == "DEAD" and c.status == 410
    out, evidence = citations.render([c], [])
    assert evidence == ""  # nothing from a dead page
    assert 'wire_search(query="old post example.com")' in out and "sub-agent" in out


def test_unreachable_host_is_dead():
    c, _ = check({}, "https://nope.invalid/x", pool=FakePool(archived=False))
    assert c.verdict == "DEAD" and c.status is None and c.note == "unreachable"


def test_bot_blocked_page_is_blocked_not_dead():
    c, _ = check({"example.com": gone(403)}, "https://example.com/paywalled", pool=FakePool())
    assert c.verdict == "BLOCKED" and c.status == 403 and c.archive is None


def test_soft_404_by_title_and_by_home_page_redirect():
    c, _ = check({"example.com": page(NOT_FOUND)}, "https://example.com/gone", pool=FakePool(False))
    assert c.verdict == "SOFT_404" and c.note == "page says not found"
    home = fetched(ARTICLE, url="https://example.com/")
    c, _ = check({"example.com": home}, "https://example.com/deep/link", pool=FakePool())
    assert c.verdict == "ARCHIVED" and c.note == "redirected to the site's home page"


def test_long_article_about_404_errors_is_not_soft_404():
    html = (
        "<html><head><title>Fixing HTTP errors</title></head><body><article>"
        + "".join(
            f"<p>Step {i}: how to debug a 404 page not found error in nginx.</p>" for i in range(30)
        )
        + "</article></body></html>"
    )
    c, _ = check({"example.com": page(html)}, "https://example.com/blog/404-errors")
    assert c.verdict == "OK"


def test_quote_missing_and_quote_check_can_be_skipped():
    url = "https://example.com/rust"
    c, _ = check({"example.com": page(ARTICLE)}, url, quote="Rust is being discontinued now")
    assert c.verdict == "QUOTE_MISSING" and c.quote
    c, _ = check({"example.com": page(ARTICLE)}, url, quote="Rust is being discontinued now",
                 check_quotes=False)  # fmt: skip
    assert c.verdict == "OK"


def test_pdf_is_ok_without_parsing():
    pdf = page(b"%PDF-1.7 ...", ctype="application/pdf")
    c, _ = check({"example.com": pdf}, "https://example.com/paper.pdf")
    assert c.verdict == "OK" and c.excerpt.startswith("(application/pdf")


# ---- wire_cite -----------------------------------------------------------------------------------


def make_wire(tmp_path, routes):
    w = Wire(home=tmp_path)
    w.ctx = Context(pool=None, waterfall=FakeWaterfall(routes), vault=w.vault, ledger=w.ledger)
    return w


def test_cite_flags_links_never_seen_until_read(tmp_path):
    w = make_wire(tmp_path, {"example.com": page(ARTICLE)})
    answer = "Rust 2.0 is out ([source](https://example.com/rust))."
    first = run(w.cite(answer))
    assert first.startswith("[WIRE | cite | 1 links: 1 ok | 1 never read via agent-wire |")
    assert (
        "- OK https://example.com/rust · NOT SEEN" in first and "never returned this link" in first
    )
    run(w.read("https://example.com/rust"))
    second = run(w.cite(answer))
    assert "never read" not in second and "never returned" not in second
    report, evidence = second.split("[UNTRUSTED_EXTERNAL_DATA:", 1)
    assert "- OK https://example.com/rust" in report and "Rust 2.0 released" not in report
    assert "Rust 2.0 released" in evidence and evidence.endswith("[/UNTRUSTED_EXTERNAL_DATA]")
    run(w.close())


def test_cite_reports_unsourced_claims_and_handles_empty_input(tmp_path):
    w = make_wire(tmp_path, {})
    out = run(w.cite("Rust has 4 million users [[UNSOURCED]]."))
    assert "| 0 links | 1 unsourced |" in out
    assert "## Unsourced claims (1)\n- Rust has 4 million users" in out
    assert "Never invent a source" in out and "UNTRUSTED_EXTERNAL_DATA" not in out
    assert "No links and no [[UNSOURCED]] markers found" in run(w.cite("plain text"))
    assert "1 links" in run(w.cite(["https://nope.invalid/a"]))
    run(w.close())


def test_cite_caps_link_count(tmp_path):
    w = make_wire(tmp_path, {"example.com": page(ARTICLE)})
    text = "\n".join(f"https://example.com/p{i}" for i in range(citations.MAX_CITATIONS + 3))
    out = run(w.cite(text))
    assert f"{citations.MAX_CITATIONS} links" in out and "3 more not checked" in out
    run(w.close())


def test_mcp_wire_cite_tool(tmp_path):
    w = make_wire(tmp_path, {"example.com": gone(404)})
    mcp = create_server(wire=w)

    async def go():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            res = await c.call_tool("wire_cite", {"text": "See https://example.com/x"})
            return tools, res

    tools, res = run(go())
    assert tools["wire_cite"].annotations.read_only_hint is True
    assert "DEAD (HTTP 404) https://example.com/x" in res.content[0].text


# ---- research link check -------------------------------------------------------------------------


def test_research_marks_dead_outbound_links(tmp_path):
    from test_research import routes

    rts = routes()
    rts["wire.dev/post"] = gone(404)
    w = make_wire(tmp_path, rts)
    r = run(Researcher(w).run("agent web access", sources=["hn", "news"], depth="quick",
                              verify_links=True))  # fmt: skip
    assert r.link_status["https://wire.dev/post"].verdict == "DEAD"
    md = briefmod.to_markdown(r)
    assert "[article](https://wire.dev/post) ⚠ dead (HTTP 404)" in md
    assert "_Links checked:" in md
    data = briefmod.to_dict(r)
    assert data["link_status"]["https://wire.dev/post"]["status"] == 404
    probed = [u for u, _ in w.ctx.waterfall.calls if "wire.dev" in u]
    assert probed == ["https://wire.dev/post"]
    # off by default in the engine; the Wire entry point turns it on
    w.ctx.waterfall.calls.clear()
    r = run(Researcher(w).run("agent web access", sources=["hn", "news"], depth="quick"))
    assert not r.link_status and not any("wire.dev" in u for u, _ in w.ctx.waterfall.calls)
    out = run(w.research("agent web access", platforms=["hn", "news"], depth="quick",
                         verify_links=False))  # fmt: skip
    assert "⚠" not in out
    run(w.close())
