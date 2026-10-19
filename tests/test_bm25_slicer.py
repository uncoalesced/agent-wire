from agent_wire.compression.bm25_slicer import (
    count_tokens,
    rank_comments,
    slice_text,
    split_blocks,
)
from agent_wire.models import WireComment

FILLER = "The weather was mild and the market opened without much movement today. " * 12


def make_doc() -> str:
    blocks = ["# Ryzen review\n\nTL;DR: fast chip, fine thermals."]
    blocks += [f"## Section {i}\n\n{FILLER}" for i in range(20)]
    blocks.append(
        "## Battery\n\nBattery life on the Ryzen AI 7 350 reached eleven hours in our test."
    )
    return "\n\n".join(blocks)


def test_under_budget_unchanged():
    assert slice_text("short text", "q", 100) == "short text"


def test_query_relevant_tail_block_kept_and_in_order():
    doc = make_doc()
    out = slice_text(doc, "battery life hours", 300)
    assert count_tokens(out) <= 300
    assert out.startswith("# Ryzen review")  # lead preserved
    assert "eleven hours" in out
    assert out.index("TL;DR") < out.index("eleven hours")


def test_no_query_falls_back_to_top_down():
    out = slice_text(make_doc(), None, 300)
    assert "Section 0" in out and "Section 19" not in out


def test_single_giant_block_truncated():
    text = "word " * 5000
    assert count_tokens(slice_text(text, "word", 100)) <= 100


def test_split_blocks_keeps_fences_and_headings():
    text = "# Title\n\nintro\n\n## Code\n\n```py\na = 1\n\nb = 2\n```\n\nafter"
    assert split_blocks(text) == [
        "# Title\n\nintro",
        "## Code\n\n```py\na = 1\n\nb = 2\n```",
        "after",
    ]


def test_rank_comments_prefers_signal():
    noise = [WireComment(id=str(i), body=FILLER, score=1) for i in range(10)]
    accepted = WireComment(
        id="acc", body="Use the --profile local flag.", score=5, is_accepted=True
    )
    upvoted = WireComment(id="up", body="Works great with Ollama.", score=900)
    comments = noise[:5] + [accepted] + noise[5:] + [upvoted]
    kept = rank_comments(comments, "profile flag", max_tokens=200)
    ids = [c.id for c in kept]
    assert "acc" in ids and "up" in ids
    assert ids.index("acc") < ids.index("up")  # original order


def test_encoding_survives_a_concurrent_first_download(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_wire.compression import bm25_slicer

    monkeypatch.setenv("WIRE_HOME", str(tmp_path))
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    (tmp_path / "tiktoken").mkdir()
    (tmp_path / "tiktoken" / "abc.123.tmp").write_bytes(b"partial")
    calls = []

    def racy(name):
        calls.append(name)
        if len(calls) == 1:
            raise FileExistsError(
                "[WinError 183] Cannot create a file when that file already exists"
            )
        return SimpleNamespace(name=name)  # offline: no real download

    monkeypatch.setattr(bm25_slicer.tiktoken, "get_encoding", racy)
    bm25_slicer._encoding.cache_clear()
    try:
        assert bm25_slicer._encoding().name == "o200k_base" and len(calls) == 2
        assert not list((tmp_path / "tiktoken").glob("*.tmp"))
    finally:
        bm25_slicer._encoding.cache_clear()
