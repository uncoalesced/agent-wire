"""Regression tests for audited citation-check bugs (offline)."""

import json

from conftest import fetched
from test_citations import ARTICLE, FakePool, check, gone, make_wire, page, run
from test_research import routes

from agent_wire.research import brief as briefmod
from agent_wire.research.engine import Researcher
from agent_wire.verify import citations

# ---- quote matching ------------------------------------------------------------------------------


def test_negated_or_altered_quotes_do_not_pass():
    body = "Our trial found the drug is safe for children under 5 years old, doctors say."
    assert citations.quote_found("the drug is safe for children under 5 years old", body)
    assert not citations.quote_found("The drug is not safe for children under 5 years old", body)
    assert not citations.quote_found("the drug isn't safe for children under 5 years old", body)
    assert not citations.quote_found("the drug is safe for children under 6 years old", body)
    negated_page = "the drug is not safe for children under 5 years old"
    assert not citations.quote_found(
        "the drug is safe for children under 5 years old", negated_page
    )
    assert citations.quote_found("the drug isn't safe for children under 5 years old", negated_page)


def test_words_in_the_wrong_order_do_not_pass():
    body = "dogs chase cats while birds watch the whole scene unfold quietly"
    assert not citations.quote_found("cats chase dogs while the whole scene watch birds", body)


def test_exact_match_is_whole_word():
    assert not citations.quote_found("cat sat down here today", "a bobcat sat down here today")
    assert citations.quote_found("cat sat down here today", "so the cat sat down here today ok")


def test_quote_belongs_to_the_nearest_link_only():
    text = 'See https://a.example/one and "the quick brown fox jumps over" https://b.example/two'
    one, two = citations.extract_citations(text)
    assert one.quote is None and two.quote == "the quick brown fox jumps over"
    text = 'Per [a](https://a.example/x) "the quick brown fox jumps over" then [b](https://b.example/y).'
    a, b = citations.extract_citations(text)
    assert a.quote == "the quick brown fox jumps over" and b.quote is None


# ---- non-HTML bodies -----------------------------------------------------------------------------


def test_pdf_with_a_quote_says_it_was_not_checked():
    pdf = page(b"%PDF-1.7 ...", ctype="application/pdf")
    c, _ = check({"example.com": pdf}, "https://example.com/p.pdf", quote="a quote that is long")
    assert c.verdict == "OK" and c.note == "quote not checked: application/pdf"
    out, _ = citations.render([c], [])
    assert "quote not checked: application/pdf" in out
    c, _ = check({"example.com": pdf}, "https://example.com/p.pdf")
    assert c.note == ""


def test_json_quotes_are_checked():
    body = json.dumps({"text": "the committee approved the budget on Tuesday morning"})
    rts = {"example.com": page(body, ctype="application/json")}
    ok, _ = check(rts, "https://example.com/a", quote="approved the budget on Tuesday morning")
    assert ok.verdict == "OK" and not ok.note
    miss, _ = check(rts, "https://example.com/a", quote="rejected the budget on Tuesday morning")
    assert miss.verdict == "QUOTE_MISSING"


# ---- soft 404 false positives --------------------------------------------------------------------


def test_long_article_titled_about_404_is_not_soft_404():
    html = (
        "<html><head><title>How to fix a 404 error in nginx</title></head><body><article>"
        + "".join(
            f"<p>Step {i}: nginx returns a 404 when no location block matches.</p>"
            for i in range(30)
        )
        + "</article></body></html>"
    )
    c, _ = check({"example.com": page(html)}, "https://example.com/blog/nginx-404")
    assert c.verdict == "OK"


def test_long_page_that_opens_with_not_found_is_still_soft_404():
    html = (
        "<html><head><title>404 Not Found</title></head><body><article><p>Page not found.</p>"
        + "<p>Try the menu, search or popular posts below for something else to read.</p>" * 20
        + "</article></body></html>"
    )
    c, _ = check({"example.com": page(html)}, "https://example.com/gone", pool=FakePool(False))
    assert c.verdict == "SOFT_404"


def test_index_html_and_locale_redirects_to_root_are_the_same_page():
    for requested in ("https://example.com/index.html", "https://example.com/en/",
                      "https://example.com/home"):  # fmt: skip
        home = fetched(ARTICLE, url="https://example.com/")
        c, _ = check({"example.com": home}, requested, pool=FakePool())
        assert c.verdict == "OK", requested


# ---- unreachable ---------------------------------------------------------------------------------


def timeout(url, kw):
    raise TimeoutError


def test_timeouts_and_server_errors_are_unreachable_not_dead():
    pool = FakePool(archived=True)
    for rts in ({"example.com": timeout}, {"example.com": gone(500)}, {"example.com": gone(502)}):
        c, _ = check(rts, "https://example.com/x", pool=pool)
        assert c.verdict == "UNREACHABLE" and c.archive is None
    assert not pool.calls  # no archive lookup for a page that may be fine
    assert (
        check({"example.com": gone(503)}, "https://example.com/x", pool=pool)[0].verdict
        == "BLOCKED"
    )
    assert (
        check({"example.com": gone(404)}, "https://example.com/x", pool=pool)[0].verdict
        == "ARCHIVED"
    )
    c, _ = check({"example.com": gone(500)}, "https://example.com/x")
    out, _ = citations.render([c], [])
    assert "UNREACHABLE (HTTP 500)" in out and "retry later" in out and "alternative" not in out
    assert "1 unreachable" in citations.summary([c], [])
    assert not c.bad


def test_research_brief_says_unreachable_not_dead(tmp_path):
    rts = routes()
    rts["wire.dev/post"] = gone(502)
    w = make_wire(tmp_path, rts)
    r = run(Researcher(w).run("agent web access", sources=["hn", "news"], depth="quick",
                              verify_links=True))  # fmt: skip
    assert r.link_status["https://wire.dev/post"].verdict == "UNREACHABLE"
    md = briefmod.to_markdown(r)
    assert (
        "[article](https://wire.dev/post) ⚠ unreachable (HTTP 502; could not reach it, retry)" in md
    )
    assert "wire.dev/post) ⚠ dead" not in md and "1 unreachable (retry)" in md
    run(w.close())


def test_private_urls_are_never_sent_to_the_archive():
    pool = FakePool()
    checker = citations.Checker(None, pool)
    assert run(checker._wayback("http://10.0.0.5/secret")) == (None, False)
    assert run(checker._wayback("http://localhost:8080/x")) == (None, False)
    assert not pool.calls
