"""Query-aware BM25 + social-signal block ranking that packs the best blocks into a token budget."""

import math
import os
import re
from collections import Counter
from functools import cache

import tiktoken

from agent_wire.config import init_wire_home
from agent_wire.models import WireComment

HEADER_TOKENS = 200
K1, B = 1.5, 0.75
W_ACCEPTED, W_OP = 3.0, 1.5

_WORD = re.compile(r"\w+")
_HEADING_ONLY = re.compile(r"#{1,6}\s+[^\n]*")


@cache
def _encoding() -> tiktoken.Encoding:
    # tiktoken caches its BPE file in the OS temp dir by default; keep it in WIRE_HOME instead.
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(init_wire_home() / "tiktoken"))
    return tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def truncate_tokens(text: str, max_tokens: int) -> str:
    tokens = _encoding().encode(text, disallowed_special=())
    return text if len(tokens) <= max_tokens else _encoding().decode(tokens[:max_tokens])


def split_blocks(text: str) -> list[str]:
    """Paragraph blocks; fenced code stays whole and a lone heading joins the block after it."""
    blocks, buf, fence = [], [], None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence:
            buf.append(line)
            if stripped.startswith(fence):
                fence = None
        elif stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            buf.append(line)
        elif not stripped:
            if buf:
                blocks.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append("\n".join(buf))

    merged, heading = [], None
    for block in blocks:
        if heading:
            block, heading = f"{heading}\n\n{block}", None
        if _HEADING_ONLY.fullmatch(block):
            heading = block
        else:
            merged.append(block)
    if heading:
        merged.append(heading)
    return merged


def bm25_scores(blocks: list[str], query: str) -> list[float]:
    terms = set(_WORD.findall(query.lower()))
    if not terms or not blocks:
        return [0.0] * len(blocks)
    docs = [Counter(_WORD.findall(b.lower())) for b in blocks]
    n = len(docs)
    avgdl = sum(sum(d.values()) for d in docs) / n or 1.0
    idf = {}
    for t in terms:
        df = sum(1 for d in docs if t in d)
        idf[t] = math.log((n - df + 0.5) / (df + 0.5) + 1)
    scores = []
    for d in docs:
        dl = sum(d.values())
        s = 0.0
        for t in terms:
            if tf := d.get(t):
                s += idf[t] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / avgdl))
        scores.append(s)
    return scores


def select(costs: list[int], scores: list[float], budget: int) -> list[int]:
    """Greedy pack by score (ties keep document order); returns kept indices in document order."""
    kept, used = [], 0
    for i in sorted(range(len(costs)), key=lambda i: -scores[i]):
        if used + costs[i] <= budget:
            kept.append(i)
            used += costs[i]
    return sorted(kept)


def slice_text(text: str, query: str | None = None, max_tokens: int = 3000) -> str:
    if count_tokens(text) <= max_tokens:
        return text
    blocks = split_blocks(text)
    if len(blocks) == 1:
        return truncate_tokens(text, max_tokens)

    # Always keep the lead (title / TL;DR / first ~200 tokens), even if it isn't query-relevant.
    lead_budget = min(HEADER_TOKENS, max_tokens)
    costs = [count_tokens(b) + 1 for b in blocks]  # +1 for the joining blank line
    lead, used = 0, 0
    while lead < len(blocks) and used + costs[lead] <= lead_budget:
        used += costs[lead]
        lead += 1
    if lead == 0:
        blocks[0] = truncate_tokens(blocks[0], lead_budget)
        used, lead = count_tokens(blocks[0]) + 1, 1

    rest = blocks[lead:]
    kept = select(costs[lead:], bm25_scores(rest, query or ""), max_tokens - used)
    return "\n\n".join(blocks[:lead] + [rest[i] for i in kept])


def _subtree_text(c: WireComment) -> str:
    return "\n".join([c.body, *(_subtree_text(r) for r in c.replies)])


def rank_comments(
    comments: list[WireComment], query: str | None = None, max_tokens: int = 1500
) -> list[WireComment]:
    """Keep the highest-signal top-level comment sub-trees that fit, in original order."""
    texts = [_subtree_text(c) for c in comments]
    bm = bm25_scores(texts, query or "")
    scores = [
        bm[i] + W_ACCEPTED * c.is_accepted + W_OP * c.is_op + math.log1p(max(0, c.score))
        for i, c in enumerate(comments)
    ]
    kept = select([count_tokens(t) for t in texts], scores, max_tokens)
    return [comments[i] for i in kept]
