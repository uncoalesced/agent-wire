"""Strip tracking params from URLs and fold repeated Markdown links into reference links."""

import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"utm_\w*|fbclid|gclid", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")
_CODE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`")


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not _TRACKING.fullmatch(k)]
    if len(kept) == len(pairs):
        return url  # untouched: avoid re-encoding churn
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _clean_match(m: re.Match) -> str:
    url = m[0].rstrip(".,;:!?")
    return clean_url(url) + m[0][len(url) :]


def compress_links(markdown: str) -> str:
    counts = Counter(m[2] for m in _MD_LINK.finditer(markdown))
    if not any(n > 1 for n in counts.values()):
        return markdown
    ids: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        if counts[m[2]] < 2:
            return m[0]
        return f"[{m[1]}][{ids.setdefault(m[2], len(ids) + 1)}]"

    body = _MD_LINK.sub(repl, markdown)
    refs = "\n".join(f"[{n}]: {url}" for url, n in ids.items())
    return f"{body}\n\n{refs}"


def compact(text: str) -> str:
    """Clean and fold links outside code; fenced and inline code pass through untouched."""
    code: list[str] = []

    def stash(m: re.Match) -> str:
        code.append(m[0])
        return f"\x00{len(code) - 1}\x00"

    out = compress_links(_URL.sub(_clean_match, _CODE.sub(stash, text)))
    return re.sub(r"\x00(\d+)\x00", lambda m: code[int(m[1])], out)
