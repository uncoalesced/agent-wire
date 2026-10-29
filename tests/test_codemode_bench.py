"""Code Mode vs classic tool calls on the same task, offline. Run with -s to see the numbers.

Task: "What are HN's top 4 threads on rust async saying? Give each title, its points, and the
3 highest-voted comments in one line each." Tokens count what the model writes (tool arguments
or the script) plus everything the tools send back into its context.
"""

import asyncio
import json

from conftest import FakeWaterfall

from agent_wire.channels.base import Context
from agent_wire.compression.bm25_slicer import count_tokens
from agent_wire.server import Wire

SENTENCES = [
    "I moved our ingest service from threads to tokio and p99 latency dropped by a third.",
    "Pinning still confuses half my team; the error messages have improved but not enough.",
    "Function colouring is the real cost, every sync helper ends up with an async twin.",
    "Cancellation safety bit us in production when a select! branch dropped a half-read frame.",
    "For CPU-bound work just use rayon and keep the async runtime for IO, it is simpler.",
    "Async closures landing in stable removed most of our boxed future boilerplate.",
]


def item(n):
    kids = [{"id": n * 100 + i, "author": f"user{i}", "points": 50 - i,
             "text": " ".join(SENTENCES[(i + j) % len(SENTENCES)] for j in range(3)),
             "children": []} for i in range(40)]  # fmt: skip
    return {"id": n, "title": f"Rust async story {n}", "author": "op", "points": 300 - n,
            "text": "<p>" + " ".join(SENTENCES) * 4 + "</p>", "children": kids}  # fmt: skip


SEARCH = {"hits": [{"objectID": str(n), "title": f"Rust async story {n}", "points": 300 - n,
                    "num_comments": 40, "author": "op", "created_at_i": 1_790_000_000}
                   for n in range(1, 5)]}  # fmt: skip
ROUTES = {"hn.algolia.com/api/v1/search": SEARCH,
          **{f"hn.algolia.com/api/v1/items/{n}": item(n) for n in range(1, 5)}}  # fmt: skip

SCRIPT = """\
const hits = await wire.search('rust async', {platforms: 'hn', limit: 4});
const docs = await wire.read(hits.map(h => h.url));
return docs.map(d => ({title: d.title, points: d.score,
  top: d.comments.sort((a, b) => b.score - a.score).slice(0, 3).map(c => c.text.slice(0, 90))}));"""


def classic(w: Wire) -> int:
    """search, then one batched read of every hit (the cheapest classic path)."""
    search_args = {"query": "rust async", "platforms": ["hn"], "limit": 4, "auto_expand_top": 0}
    out = asyncio.run(w.search(**search_args))
    urls = [f"https://news.ycombinator.com/item?id={n}" for n in range(1, 5)]
    read_args = {"urls": urls}
    out2 = asyncio.run(w.read(**read_args))
    return sum(count_tokens(x) for x in (json.dumps(search_args), out, json.dumps(read_args), out2))


def code_mode(w: Wire) -> tuple[int, str]:
    out = asyncio.run(w.run(SCRIPT))
    return count_tokens(SCRIPT) + count_tokens(out), out


def make_wire(tmp_path, sub):
    w = Wire(home=tmp_path / sub)
    w.ctx = Context(pool=None, waterfall=FakeWaterfall(ROUTES), vault=w.vault, ledger=w.ledger)
    return w


def test_code_mode_uses_fewer_context_tokens(tmp_path):
    before = classic(make_wire(tmp_path, "a"))
    after, out = code_mode(make_wire(tmp_path, "b"))
    assert "Rust async story 1" in out and "[WIRE | code | 2 calls" in out
    print(f"\nclassic tools: {before} tokens · code mode: {after} tokens · "
          f"{round(100 * (1 - after / before))}% fewer\n{out.splitlines()[0]}")  # fmt: skip
    assert after < before * 0.6
