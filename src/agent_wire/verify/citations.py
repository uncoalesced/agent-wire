"""Citation checks: [[UNSOURCED]] canaries, link probes (dead / archived / blocked / soft 404),
quoted-text checks, and whether Agent Wire ever saw the link this session."""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi.requests.exceptions import RequestException

from agent_wire.channels.tier1_global.web import extract_article
from agent_wire.engine.waterfall import WireFetchError, wayback_snapshot

CANARY = "[[UNSOURCED]]"
CDX = "https://web.archive.org/cdx/search/cdx"
MAX_CITATIONS = 25
CONCURRENCY = 8
TIMEOUT_S = 15
EXCERPT_CHARS = 200
QUOTE_OVERLAP = 0.8  # share of a quote's words that must sit together on the page
MIN_QUOTE_WORDS = 4
BLOCK_STATUSES = frozenset({401, 403, 429, 503})
SHORT_BODY = (
    400  # not-found pages say so in a line or two; longer pages that mention 404 are articles
)

_CANARY = re.compile(r"\[\[\s*unsourced\s*\]\]", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# Balanced single-level parentheses keep Wikipedia-style URLs like /Foo_(bar) whole.
_URL_BODY = r"https?://(?:[^\s()<>\[\]\"'`]|\([^\s()<>]*\))+"
_MD_LINK = re.compile(r"\[[^\]]*\]\((" + _URL_BODY + r")\)")
_BARE = re.compile(_URL_BODY)
_QUOTE = re.compile(r"[\"“]([^\"“”]{12,})[\"”]")
_TRAILING = ".,;:!?*_'\""
_WORD = re.compile(r"\w+")
_SOFT_404_TITLE = re.compile(
    r"\b404\b|\bnot found\b|page (?:does not|doesn't) exist|no longer available|page unavailable",
    re.IGNORECASE,
)
_SOFT_404_BODY = re.compile(
    r"\b404\b|page not found|page (?:you (?:requested|are looking for)|cannot be found)"
    r"|(?:does not|doesn't) exist|no longer available|has been removed",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Citation:
    url: str
    quote: str | None = None


@dataclass(slots=True)
class Check:
    url: str
    verdict: str  # OK, SOFT_404, ARCHIVED, DEAD, BLOCKED, QUOTE_MISSING
    status: int | None = None
    title: str | None = None
    excerpt: str = ""
    archive: str | None = None
    quote: str | None = None
    note: str = ""
    seen: bool = True

    @property
    def bad(self) -> bool:
        return self.verdict in ("SOFT_404", "ARCHIVED", "DEAD")


def normalize_url(url: str) -> str:
    """Key for 'same link': lowercase host, no www, fragment, utm_* or trailing slash."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip()
    host = (parts.hostname or "").lower().removeprefix("www.")
    if port:
        host += f":{port}"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not k.lower().startswith("utm_")]
    )  # fmt: skip
    return urlunsplit(("https", host, parts.path.rstrip("/"), query, ""))


def find_urls(text: str) -> list[str]:
    return [m.rstrip(_TRAILING) for m in _BARE.findall(text or "")]


def find_canaries(text: str) -> list[str]:
    """The claim each [[UNSOURCED]] marker sits on: the last sentence before it on its line."""
    claims = []
    for line in (text or "").splitlines():
        pieces = _CANARY.split(line)
        for before in pieces[:-1]:
            sentences = [s for s in _SENTENCE_END.split(before.strip()) if s.strip()]
            claim = sentences[-1].strip(" -*•>()[]") if sentences else ""
            claims.append(claim or "(marker with no claim text on its line)")
    return claims


def extract_citations(text: str) -> list[Citation]:
    """Every distinct link in the text, with a quoted passage when one sits on the same line."""
    out: dict[str, Citation] = {}
    for line in (text or "").splitlines():
        quote = next((q.strip() for q in _QUOTE.findall(line)
                      if len(_WORD.findall(q)) >= MIN_QUOTE_WORDS), None)  # fmt: skip
        urls = [m.rstrip(_TRAILING) for m in _MD_LINK.findall(line)] + find_urls(line)
        for url in urls:
            key = normalize_url(url)
            if key not in out:
                out[key] = Citation(url, quote)
            elif quote and not out[key].quote:
                out[key].quote = quote
    return list(out.values())


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def quote_found(quote: str, body: str) -> bool:
    """Exact (normalised) match, else >= 80% of the quote's words inside one same-length window."""
    q, page = _words(quote), _words(body)
    if not q:
        return True
    if " ".join(q) in " ".join(page):
        return True
    need, k = Counter(q), len(q)
    if len(page) <= k:
        return sum((need & Counter(page)).values()) / k >= QUOTE_OVERLAP
    window = Counter(page[:k])
    best = sum((need & window).values())
    for i in range(k, len(page)):
        window[page[i]] += 1
        window[page[i - k]] -= 1
        if page[i] in need or page[i - k] in need:
            best = max(best, sum((need & window).values()))
            if best / k >= QUOTE_OVERLAP:
                return True
    return best / k >= QUOTE_OVERLAP


def search_hint(url: str, title: str | None = None) -> str:
    """A wire_search query for a replacement: the page title, else the words in the URL slug."""
    parts = urlsplit(url)
    if title:
        words = title
    else:
        slug = re.sub(r"\.[a-z0-9]{2,5}$", "", parts.path, flags=re.IGNORECASE)
        words = " ".join(w for w in re.split(r"[/\-_.+%]+", slug) if w.isalpha() and len(w) > 1)
    host = (parts.hostname or "").removeprefix("www.")
    return f"{words} {host}".strip()


def readable_archive(raw: str) -> str:
    """Wayback `id_` (raw bytes) URL -> the normal page URL people can open."""
    return re.sub(r"(/web/\d{1,14})id_/", r"\1/", raw)


def _same_path(a: str, b: str) -> bool:
    return urlsplit(a).path.rstrip("/") == urlsplit(b).path.rstrip("/")


def _is_html(content_type: str, text: str) -> bool:
    return "html" in content_type or text.lstrip()[:1] == "<"


class Checker:
    def __init__(self, waterfall, pool, timeout_s: float = TIMEOUT_S):
        self.waterfall = waterfall
        self.pool = pool
        self.timeout_s = timeout_s
        self.sem = asyncio.Semaphore(CONCURRENCY)

    async def check_all(
        self, citations: list[Citation], *, check_quotes: bool = True
    ) -> list[Check]:
        return list(await asyncio.gather(*(self.check(c, check_quotes=check_quotes)
                                           for c in citations)))  # fmt: skip

    async def check(self, c: Citation, *, check_quotes: bool = True) -> Check:
        async with self.sem:
            return await self._check(c, check_quotes)

    async def _check(self, c: Citation, check_quotes: bool) -> Check:
        try:
            # No archive fallback: a Wayback copy would hide that the original is gone.
            f = await asyncio.wait_for(
                self.waterfall.fetch(c.url, archive=False, browser=False), self.timeout_s
            )
        except WireFetchError as e:
            m = re.search(r"HTTP (\d{3})", str(e))
            status = int(m[1]) if m else None
            if status in BLOCK_STATUSES:
                return Check(c.url, "BLOCKED", status, note="site refuses automated readers")
            return await self._gone(c, status, f"HTTP {status}" if status else "unreachable")
        except (RequestException, TimeoutError, OSError) as e:
            return await self._gone(c, None, f"unreachable ({type(e).__name__})")

        ctype = f.content_type
        if ctype and "html" not in ctype and not ctype.startswith("text/"):
            size = f"{len(f.content) // 1024} KB"
            return Check(c.url, "OK", f.status, excerpt=f"({ctype}, {size})")
        text = f.text
        body, info = extract_article(text) if _is_html(ctype, text) else (text, {})
        title = (info.get("title") or "").strip() or None
        flat = " ".join(body.split())
        if self._soft_404(c.url, f.url, title, flat):
            moved = f.url and not _same_path(c.url, f.url)
            check = await self._gone(
                c,
                f.status,
                "redirected to the site's home page" if moved else "page says not found",
            )
            if check.verdict == "DEAD":
                check.verdict = "SOFT_404"
            check.title = title
            return check
        check = Check(c.url, "OK", f.status, title=title, excerpt=flat[:EXCERPT_CHARS])
        if check_quotes and c.quote and not quote_found(c.quote, body):
            check.verdict, check.quote = "QUOTE_MISSING", c.quote
        return check

    @staticmethod
    def _soft_404(requested: str, final: str, title: str | None, flat: str) -> bool:
        req, fin = urlsplit(requested), urlsplit(final or requested)
        if req.path.strip("/") and not fin.path.strip("/") and not fin.query:
            return True  # a deep link bounced to the home page
        if title and _SOFT_404_TITLE.search(title):
            return True
        return len(flat) < SHORT_BODY and bool(_SOFT_404_BODY.search(flat[:400]))

    async def _gone(self, c: Citation, status: int | None, note: str) -> Check:
        raw, looked = await self._wayback(c.url)
        if raw:
            return Check(c.url, "ARCHIVED", status, archive=readable_archive(raw), note=note)
        if not looked:
            note = "; ".join(
                n for n in (note, "archive lookup failed, a copy may still exist") if n
            )
        return Check(c.url, "DEAD", status, note=note)

    async def _wayback(self, url: str) -> tuple[str | None, bool]:
        """(snapshot URL, whether the archive actually answered). The availability API is
        rate-limited and often answers empty, so the CDX index is the fallback."""
        if self.pool is None:
            return None, False
        try:
            if raw := await asyncio.wait_for(wayback_snapshot(self.pool, url), self.timeout_s):
                return raw, True
        except (TimeoutError, RequestException, OSError):
            pass
        try:
            f = await asyncio.wait_for(
                self.pool.get(CDX, params={"url": url, "output": "json", "limit": "-1",
                                           "filter": "statuscode:200",
                                           "fl": "timestamp,original"}, impersonate=None),
                self.timeout_s,
            )  # fmt: skip
            rows = f.json() if f.ok else None
        except (TimeoutError, RequestException, OSError, ValueError):
            return None, False
        if not isinstance(rows, list):
            return None, False
        if len(rows) < 2:
            return None, True  # the index answered: no capture
        ts, original = rows[-1][:2]
        return f"https://web.archive.org/web/{ts}/{original}", True


_ADVICE = {
    "OK": "",
    "QUOTE_MISSING": "the page is live but the quoted text is not on it; drop the quote or find "
    "the page that really says it",
    "BLOCKED": "the page exists but blocks automated readers, so its contents are unverified; "
    "keep it and say so",
    "ARCHIVED": "the original is gone; cite the archived copy and say the original link is dead",
    "SOFT_404": "the page no longer has this content; replace it",
    "DEAD": "the link is dead and has no archived copy; replace it",
}


def summary(checks: list[Check], claims: list[str]) -> str:
    counts = Counter(c.verdict for c in checks)
    bits = [f"{n} {v.lower()}" for v, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    out = f"{len(checks)} links" + (f": {', '.join(bits)}" if bits else "")
    if unseen := sum(1 for c in checks if not c.seen):
        out += f" | {unseen} never read via agent-wire"
    if claims:
        out += f" | {len(claims)} unsourced"
    return out


def render(checks: list[Check], claims: list[str], dropped: int = 0) -> tuple[str, str]:
    """(report, evidence). The report holds only the agent's own text, URLs, verdicts and advice,
    so it can sit outside the untrusted block; page titles and excerpts go in `evidence`."""
    lines = []
    if claims:
        lines.append(f"## Unsourced claims ({len(claims)})")
        lines += [f"- {claim}" for claim in claims]
        lines.append(
            "→ find a source for each (wire_search), or tell the user plainly that the claim is "
            "unverified. Never invent a source to replace the marker."
        )
    if checks:
        lines += ["", "## Links"] if lines else ["## Links"]
    for c in checks:
        status = f" (HTTP {c.status})" if c.status and c.verdict != "OK" else ""
        note = c.note if c.note and c.note != f"HTTP {c.status}" else ""
        tags = [t for t in (note, "" if c.seen else "NOT SEEN") if t]
        lines.append(f"- {c.verdict}{status} {c.url}" + "".join(f" · {t}" for t in tags))
        if c.archive:
            lines.append(f"  archived copy: {c.archive}")
        if c.quote:
            lines.append(f'  quote not on page: "{c.quote[:160]}"')
        if advice := _ADVICE[c.verdict]:
            lines.append(f"  → {advice}")
        if c.verdict in ("DEAD", "SOFT_404"):
            lines.append(
                f'  → alternative: wire_search(query="{search_hint(c.url)}"), or ask '
                "the user / a sub-agent for a replacement. Do not drop the citation silently."
            )
    if any(not c.seen for c in checks):
        lines.append(
            "\nNOT SEEN = agent-wire never returned this link this session. If it came from "
            "memory, read it with wire_read and confirm it says what you claim before citing it."
        )
    if dropped:
        lines.append(f"\n_checked the first {MAX_CITATIONS} links; {dropped} more not checked_")
    if not lines:
        lines.append(
            f"No links and no {CANARY} markers found. Pass the answer text with its sources, or a "
            "list of URLs."
        )
    evidence = [
        f"- {c.url} · {c.title or 'untitled'}" + (f" · {c.excerpt}" if c.excerpt else "")
        for c in checks
        if c.title or c.excerpt
    ]
    if evidence:
        evidence.insert(
            0, "What each live page says (first lines), to confirm it matches the claim:"
        )
    return "\n".join(lines).strip(), "\n".join(evidence)
