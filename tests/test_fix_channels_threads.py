"""Comment caps and event-loop offload for the heavy HTML -> Markdown conversions."""

import threading

from conftest import fetched

from agent_wire.channels.base import MAX_COMMENTS
from agent_wire.channels.tier1_global import hackernews
from agent_wire.channels.tier2_pro import archive_bypass, publishing


def _count(comments) -> int:
    return sum(1 + _count(c.replies) for c in comments)


def test_hn_comments_are_capped_shallowest_first(make_ctx, run):
    roots = [{"id": i, "text": f"<p>c{i}</p>", "author": "a",
              "children": [{"id": 10_000 + i, "text": "<p>kid</p>", "author": "b", "children": []}]}
             for i in range(MAX_COMMENTS + 100)]  # fmt: skip
    ctx = make_ctx({"/items/1": {"title": "T", "author": "op", "children": roots}})
    doc = run(ctx.channel("hackernews").read("https://news.ycombinator.com/item?id=1"))
    assert _count(doc.comments) == MAX_COMMENTS
    assert all(not c.replies for c in doc.comments)  # top-level comments win over their replies


def test_hn_parse_keeps_small_threads_whole():
    kids = [{"id": 1, "text": "x", "children": [{"id": 2, "text": "y", "children": []}]}]
    assert _count(hackernews.parse_hn_comments(kids, None)) == 2


def test_substack_comments_are_capped():
    items = [{"id": i, "body": "hi", "children": []} for i in range(MAX_COMMENTS + 50)]
    assert len(publishing.parse_substack_comments(items)) == MAX_COMMENTS


def test_archive_extraction_runs_off_the_event_loop(make_ctx, run, monkeypatch):
    seen = []

    def spy(html):
        seen.append(threading.current_thread() is threading.main_thread())
        return "body", {"title": "t", "author": None}

    monkeypatch.setattr(archive_bypass, "extract_article", spy)
    ctx = make_ctx({"archive.ph": fetched("<html><body>x</body></html>")})
    run(ctx.channel("archive").read("https://archive.ph/abc"))
    assert seen == [False]


def test_comment_conversion_runs_off_the_event_loop(make_ctx, run, monkeypatch):
    seen = []
    real = hackernews.parse_hn_comments

    def spy(*a, **kw):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **kw)

    monkeypatch.setattr(hackernews, "parse_hn_comments", spy)
    item = {"title": "T", "author": "op", "children": [{"id": 1, "text": "x", "children": []}]}
    ctx = make_ctx({"/items/1": item})
    run(ctx.channel("hackernews").read("https://news.ycombinator.com/item?id=1"))
    assert seen and not any(seen)  # recursion re-enters the spy, all inside the worker thread
