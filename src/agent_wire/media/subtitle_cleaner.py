"""VTT / SRT / JSON3 / timedtext XML -> clean paragraphs (no timestamps, no rolling duplicates)."""

import html
import re

import orjson

_TIME = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{3}"
_CUE = re.compile(rf"^({_TIME})\s*-->\s*({_TIME})")
_INLINE_TAG = re.compile(r"<[^>]+>")
_XML_CUE = re.compile(
    r'<(?:text|p)\b[^>]*?\b(?:start|t)="([\d.]+)"[^>]*>(.*?)</(?:text|p)>', re.DOTALL
)
_SENTENCE_END = re.compile(r"[.!?。！？…]$")
CHAPTER_LINE = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s*[-–—:]?\s+(.+?)\s*$")

GAP_BREAK_S = 2.5
PARAGRAPH_CHARS = 600


def _seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    return sum(float(p) * 60**i for i, p in enumerate(reversed(parts)))


def parse_captions(raw: str | bytes) -> list[tuple[float, str]]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.lstrip("﻿").strip()
    if text.startswith("{"):
        segs = []
        for ev in orjson.loads(text).get("events", []):
            line = "".join(s.get("utf8", "") for s in ev.get("segs") or [])
            if line.strip():
                segs.append((ev.get("tStartMs", 0) / 1000, line))
        return _clean(segs)
    if text.startswith("<"):
        return _clean([(float(t) / (1000 if "." not in t and float(t) > 1e4 else 1), body)
                       for t, body in _XML_CUE.findall(text)])  # fmt: skip
    segs, start, buf = [], None, []
    for line in text.splitlines():
        if m := _CUE.match(line.strip()):
            if start is not None and buf:
                segs.append((start, " ".join(buf)))
            start, buf = _seconds(m[1]), []
        elif start is not None and line.strip() and not line.strip().isdigit():
            buf.append(line.strip())
        elif not line.strip() and start is not None and buf:
            segs.append((start, " ".join(buf)))
            start, buf = None, []
    if start is not None and buf:
        segs.append((start, " ".join(buf)))
    return _clean(segs)


def _plain(body: str) -> str:
    # YouTube timedtext XML double-encodes entities (&amp;#39;), so unescape twice.
    return " ".join(html.unescape(html.unescape(_INLINE_TAG.sub("", body))).split())


def _clean(segs: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Strip markup and drop the rolling-caption repeats auto-captions produce."""
    out: list[tuple[float, str]] = []
    last = ""
    for start, body in segs:
        line = _plain(body)
        if not line or line == last:
            continue
        if last and line.startswith(last):  # rolling cue: keep only the new tail
            line = line[len(last) :].strip()
        elif last and last.endswith(line):
            continue
        if line:
            out.append((start, line))
            last = _plain(body)
    return out


def parse_chapters(description: str) -> list[tuple[float, str]]:
    chapters = [(_seconds(m[1] + ".000"), m[2]) for ln in description.splitlines()
                if (m := CHAPTER_LINE.match(ln))]  # fmt: skip
    return chapters if chapters and chapters[0][0] == 0 else []


def to_paragraphs(segs: list[tuple[float, str]], chapters: list[tuple[float, str]] = ()) -> str:
    out, para, prev_end = [], [], None
    pending = list(chapters)
    for start, line in segs:
        while pending and start >= pending[0][0]:
            if para:
                out.append(" ".join(para))
                para = []
            out.append(f"## {pending.pop(0)[1]}")
        gap = prev_end is not None and start - prev_end > GAP_BREAK_S
        long = sum(len(p) for p in para) > PARAGRAPH_CHARS and _SENTENCE_END.search(para[-1])
        if para and (gap or long):
            out.append(" ".join(para))
            para = []
        para.append(line)
        prev_end = start
    if para:
        out.append(" ".join(para))
    return "\n\n".join(out)
