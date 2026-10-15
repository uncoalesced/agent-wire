"""#L45-L120 line slicing for code and #heading section extraction for Markdown."""

import re

CONTEXT_LINES = 5

_LINE_ANCHOR = re.compile(r"#L(\d+)(?:-L?(\d+))?$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def parse_line_anchor(url: str) -> tuple[int, int] | None:
    if not (m := _LINE_ANCHOR.search(url)):
        return None
    a, b = int(m[1]), int(m[2] or m[1])
    return min(a, b), max(a, b)


def slice_lines(text: str, start: int, end: int, context: int = CONTEXT_LINES) -> str:
    lines = text.splitlines()
    lo, hi = max(1, start - context), min(len(lines), end + context)
    width = len(str(hi))
    return "\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(lo, hi + 1))


def slugify(title: str) -> str:
    """GitHub-style heading anchor."""
    s = re.sub(r"[^\w\s-]", "", title.strip().lower())
    return s.replace(" ", "-")


def slice_section(markdown: str, section: str) -> str | None:
    """Heading named/slugged `section` through the next heading of equal or higher level."""
    target = slugify(section.lstrip("#"))
    out, level, in_fence = None, 0, False
    for line in markdown.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if out is not None:
            if m and len(m[1]) <= level:
                break
            out.append(line)
        elif m and slugify(m[2]) == target:
            out, level = [line], len(m[1])
    return "\n".join(out) if out else None
