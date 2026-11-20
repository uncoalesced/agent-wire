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


class ApproxEncoding:
    """Stand-in when the o200k_base file can't be loaded (first run with no network).

    Splits like BPE pre-tokenizers do (a word keeps its leading space; punctuation and other
    whitespace stand alone), then cuts long pieces into 6-character chunks. Errs a little high,
    which keeps budgets safe, and `decode(encode(t)) == t` so truncation still works.
    """

    name = "approx"
    _PIECE = re.compile(r" ?\w+|\s+|[^\w\s]")

    def encode(self, text: str, **_kw) -> list[str]:
        out = []
        for p in self._PIECE.findall(text):
            out += [p[i : i + 6] for i in range(0, len(p), 6)] if len(p) > 6 else [p]
        return out

    def decode(self, tokens: list[str]) -> str:
        return "".join(tokens)


@cache
def _encoding() -> "tiktoken.Encoding | ApproxEncoding":
    # tiktoken caches its BPE file in the OS temp dir by default; keep it in WIRE_HOME instead.
    cache_dir = init_wire_home() / "tiktoken"
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache_dir))
    try:
        try:
            return tiktoken.get_encoding("o200k_base")
        except FileExistsError:
            # Several first runs at once (clients starting their servers together) each download
            # the file and rename it into place; on Windows the losers' rename fails. The winner's
            # copy is there now, so read it, and drop the losers' temp files.
            for tmp in cache_dir.glob("*.tmp"):
                tmp.unlink(missing_ok=True)
            return tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001 - offline first run: budgets stay approximate, reads still work
        return ApproxEncoding()


def exact_tokens() -> bool:
    """False while token counts are approximate (the tiktoken file could not be loaded)."""
    return not isinstance(_encoding(), ApproxEncoding)


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


def _oversized_best(costs: list[int], scores: list[float], budget: int) -> int | None:
    """Index of the top-ranked item when it has signal but is too big for `budget`."""
    if not costs or budget <= 0:
        return None
    best = max(range(len(costs)), key=lambda i: (scores[i], -i))
    return best if scores[best] > 0 and costs[best] > budget else None


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
        # A long first block is cut to the lead size, unless the query points at it: then it
        # keeps up to the whole budget.
        relevant = bool(query) and bm25_scores(blocks, query)[0] > 0
        blocks[0] = truncate_tokens(blocks[0], max_tokens if relevant else lead_budget)
        used, lead = count_tokens(blocks[0]) + 1, 1

    rest = blocks[lead:]
    scores = bm25_scores(rest, query or "")
    room = max_tokens - used
    if (big := _oversized_best(costs[lead:], scores, room)) is not None:
        return "\n\n".join([*blocks[:lead], truncate_tokens(rest[big], room - 1)])
    kept = select(costs[lead:], scores, room)
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
    costs = [count_tokens(t) for t in texts]
    if (big := _oversized_best(costs, scores, max_tokens)) is not None:
        c = comments[big]
        return [c.model_copy(update={"body": truncate_tokens(c.body, max_tokens), "replies": []})]
    return [comments[i] for i in select(costs, scores, max_tokens)]
