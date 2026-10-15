"""Strip tracking params from URLs and fold repeated Markdown links into reference links."""

import re
from collections import Counter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"utm_\w*|fbclid|gclid|si|ref|spm", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\((\S+?)\)")


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
    return compress_links(_URL.sub(_clean_match, text))
