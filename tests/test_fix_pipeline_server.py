from conftest import fetched
from test_server import hn_item, make_wire, run

from agent_wire.models import WireDocument

HN = "https://news.ycombinator.com/item?id=1"


def long_page():
    return "\n\n".join(
        f"## {name}\n\n"
        + "\n\n".join(f"{name} paragraph {i} has its own distinct words." for i in range(60))
        for name in ("Pricing", "Install", "Faq", "Legal", "Misc")
    )


def test_other_query_or_budget_is_not_unchanged(tmp_path):
    w = make_wire(tmp_path, {"example.com": fetched(long_page(), ctype="text/markdown")})
    url = "https://example.com/doc.md"
    a = run(w.read(url, query="pricing", max_tokens=400))
    b = run(w.read(url, query="install", max_tokens=400))
    c = run(w.read(url, query="install", max_tokens=1200))
    assert "UNCHANGED" not in b and "UNCHANGED" not in c
    assert "Install paragraph" in b and a != b and len(c) > len(b)
    assert run(w.read(url, query="install", max_tokens=1200)).startswith("[WIRE | UNCHANGED")


def test_body_edit_with_new_comment_returns_full_body(tmp_path):
    state = {"text": "<p>original body</p>", "n": 2}

    def item(url, kw):
        d = hn_item(state["n"])
        d["text"] = state["text"]
        return fetched(d, url=url)

    w = make_wire(tmp_path, {"items/1": item})
    assert "original body" in run(w.read(HN))
    state.update(text="<p>EDITED body</p>", n=3)
    out = run(w.read(HN))
    assert "EDITED body" in out and "DELTA UPDATE" not in out


def test_wrap_empty_document(tmp_path):
    w = make_wire(tmp_path, {})
    doc = WireDocument(url="https://example.com/", channel="web", body="", raw_bytes=0)
    assert w._wrap(doc, ("", 0, 0), 1).startswith("[WIRE |")
