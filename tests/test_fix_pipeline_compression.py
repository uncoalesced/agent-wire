from agent_wire.compression.bm25_slicer import count_tokens, rank_comments, slice_text
from agent_wire.compression.url_compactor import compact
from agent_wire.models import WireComment

WIKI = "https://en.wikipedia.org/wiki/Python_(programming_language)"


def long_text(word: str, n: int) -> str:
    return " ".join(f"{word}{i}" for i in range(n))


def test_oversized_accepted_answer_is_truncated_not_dropped():
    answer = WireComment(id="a", body=long_text("step", 2500), score=40, is_accepted=True)
    small = WireComment(id="c", body="thanks", score=1)
    kept = rank_comments([small, answer], "step", max_tokens=1500)
    assert [c.id for c in kept] == ["a"]
    assert 1000 < count_tokens(kept[0].body) <= 1500


def test_relevant_long_first_block_survives_a_big_budget():
    lead = "install pricing " + long_text("lead", 480)
    filler = [f"## Part {i}\n\n" + long_text(f"w{i}x", 150) for i in range(30)]
    out = slice_text("\n\n".join([lead, *filler]), "install pricing", 1500)
    assert count_tokens(out) <= 1500
    assert "lead479" in out and count_tokens(lead) > 400


def test_parenthesised_url_links_fold_intact():
    out = compact(f"[a]({WIKI}) and [b]({WIKI})")
    assert out == f"[a][1] and [b][1]\n\n[1]: {WIKI}"


def test_ref_si_spm_kept_and_utm_stripped_outside_code():
    text = (
        "Call `https://api.ex.com/v1?ref=dev` or\n```\ncurl https://api.ex.com/v1?si=1&utm_source=x\n```\n"
        "then https://ex.com/p?ref=dev&spm=2&utm_source=y&gclid=3"
    )
    out = compact(text)
    assert "`https://api.ex.com/v1?ref=dev`" in out
    assert "curl https://api.ex.com/v1?si=1&utm_source=x" in out
    assert out.endswith("https://ex.com/p?ref=dev&spm=2")
