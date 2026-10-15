from datetime import UTC, datetime, timedelta

from agent_wire.compression.anchor_slicer import parse_line_anchor, slice_lines, slice_section
from agent_wire.compression.dam_formatter import envelope, format_comments, render_document
from agent_wire.compression.url_compactor import clean_url, compact
from agent_wire.models import WireComment, WireDocument, WireTelemetry

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def test_line_anchor():
    assert parse_line_anchor("https://github.com/a/b/blob/main/x.py#L10-L50") == (10, 50)
    assert parse_line_anchor("https://github.com/a/b/blob/main/x.py#L7") == (7, 7)
    assert parse_line_anchor("https://github.com/a/b/blob/main/x.py#L9-12") == (9, 12)
    assert parse_line_anchor("https://example.com/#intro") is None


def test_slice_lines_with_context():
    text = "\n".join(f"line{i}" for i in range(1, 101))
    out = slice_lines(text, 50, 52).splitlines()
    assert out[0] == "45| line45" and out[-1] == "57| line57"
    assert slice_lines(text, 1, 1).splitlines()[0] == "1| line1"
    assert slice_lines(text, 8, 8).splitlines()[0] == " 3| line3"  # padded to widest number


def test_slice_section():
    md = (
        "# Proj\n\nintro\n\n## Getting Started\n\nstep 1\n\n### Sub\n\ndetail\n\n"
        "```sh\n# not a heading\n```\n\n## API\n\nother"
    )
    out = slice_section(md, "getting-started")
    assert out.startswith("## Getting Started") and "detail" in out and "# not a heading" in out
    assert "other" not in out
    assert slice_section(md, "Getting Started") == out
    assert slice_section(md, "missing") is None


def test_clean_url():
    assert clean_url("https://ex.com/a?utm_source=x&id=5&fbclid=y&si=z") == "https://ex.com/a?id=5"
    assert clean_url("https://ex.com/a?q=a%20b&page=2") == "https://ex.com/a?q=a%20b&page=2"


def test_compact_text_and_reference_links():
    text = (
        "See https://ex.com/p?utm_medium=x. Also [docs](https://docs.ex.com/very/long/path) "
        "and again [here](https://docs.ex.com/very/long/path), once [x](https://x.com)."
    )
    out = compact(text)
    assert "See https://ex.com/p." in out
    assert "[docs][1]" in out and "[here][1]" in out and "[x](https://x.com)" in out
    assert out.endswith("[1]: https://docs.ex.com/very/long/path")


def test_comment_tree_format():
    tree = [
        WireComment(
            id="1", body="Main point", author_handle="author_1", score=342, is_op=True,
            timestamp=NOW - timedelta(hours=2),
            replies=[WireComment(id="2", body="reply", author_handle="r", score=89,
                                 timestamp=NOW - timedelta(hours=1))],
        )
    ]  # fmt: skip
    assert format_comments(tree, NOW) == (
        "[+342 | OP] @author_1 (2h ago): Main point\n  > [+89] @r (1h ago): reply"
    )


def test_render_document_and_envelope():
    doc = WireDocument(
        url="https://ex.com",
        channel="web",
        title="Hello",
        author_handle="me",
        timestamp=NOW,
        body="Text ![logo](https://ex.com/logo.png) more\n\n\n\nend https://ex.com/?gclid=1",
        metadata={"stars": 5, "lang": None},
    )
    out = render_document(doc, NOW)
    assert out == "# Hello\n\n@me · 2026-10-15 · stars: 5\n\nText  more\n\nend https://ex.com/"
    wrapped = envelope(WireTelemetry(channel="web", tokens=480, saved_pct=78, elapsed_ms=165), out)
    assert wrapped.startswith("[WIRE | web | 480 tokens (saved 78%) | auth:none | stage:1 | 165ms]")
