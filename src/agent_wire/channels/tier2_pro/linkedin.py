"""LinkedIn: public guest job pages and search; posts/profiles via public JSON-LD or a local session."""

import re
from urllib.parse import parse_qs, quote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import (
    Channel,
    WireUnsupported,
    html_to_markdown,
    json_ld,
    meta_tags,
    ts,
)
from agent_wire.firewall.outbound_privacy import parse_json
from agent_wire.models import WireComment, WireSearchHit

GUEST = "https://www.linkedin.com/jobs-guest/jobs/api"


def job_id(url: str) -> str | None:
    p = urlsplit(url)
    if m := re.search(r"/jobs/view/(?:[\w-]*?-)?(\d{6,})", p.path):
        return m[1]
    return parse_qs(p.query).get("currentJobId", [None])[0]


def parse_job(html: str) -> dict:
    tree = LexborHTMLParser(html)

    def text(sel):
        n = tree.css_first(sel)
        return n.text(strip=True) if n else None

    desc = tree.css_first(".show-more-less-html__markup, .description__text")
    criteria = {
        (li.css_first("h3").text(strip=True) if li.css_first("h3") else ""): (
            li.css_first("span").text(strip=True) if li.css_first("span") else "")
        for li in tree.css("li.description__job-criteria-item")
    }  # fmt: skip
    return {
        "title": text("h2.top-card-layout__title, h1.top-card-layout__title, .topcard__title"),
        "company": text("a.topcard__org-name-link, .topcard__flavor a"),
        "location": text(".topcard__flavor--bullet"),
        "body": html_to_markdown(desc.html) if desc else "",
        "criteria": {k: v for k, v in criteria.items() if k},
    }


def parse_job_search(html: str) -> list[WireSearchHit]:
    hits = []
    for card in LexborHTMLParser(f"<ul>{html}</ul>").css("li"):
        a = card.css_first("a.base-card__full-link, a[href*='/jobs/view/']")
        title = card.css_first(".base-search-card__title")
        if not a or not title:
            continue
        company = card.css_first(".base-search-card__subtitle")
        loc = card.css_first(".job-search-card__location")
        hits.append(WireSearchHit(
            url=(a.attributes.get("href") or "").split("?")[0], title=title.text(strip=True),
            snippet=" · ".join(n.text(strip=True) for n in (company, loc) if n), source="linkedin"))  # fmt: skip
    return hits


def parse_public_page(html: str) -> dict:
    """Public post / profile / company pages: JSON-LD first, OpenGraph as fallback."""
    for item in json_ld(html):
        kind = item.get("@type")
        if kind in ("SocialMediaPosting", "Article", "DiscussionForumPosting"):
            author = item.get("author") or {}
            author = author[0] if isinstance(author, list) and author else author
            comments = [
                WireComment(id=str(i), body=c.get("text") or "", author_handle=(c.get("author") or {}).get("name"),
                            timestamp=ts(c.get("datePublished")))
                for i, c in enumerate(item.get("comment") or []) if isinstance(c, dict)
            ]  # fmt: skip
            stats = {s.get("interactionType", "").split("/")[-1]: s.get("userInteractionCount")
                     for s in item.get("interactionStatistic") or [] if isinstance(s, dict)}  # fmt: skip
            return {"title": item.get("headline") or (item.get("articleBody") or "")[:80],
                    "author_handle": author.get("name") if isinstance(author, dict) else None,
                    "timestamp": ts(item.get("datePublished")), "body": item.get("articleBody") or "",
                    "comments": comments, "metadata": {k: v for k, v in stats.items() if k}}  # fmt: skip
        if kind in ("Person", "Organization", "ProfilePage"):
            entity = item.get("mainEntity", item)
            return {"title": entity.get("name"), "body": entity.get("description") or "",
                    "metadata": {"followers": (entity.get("interactionStatistic") or {}).get("userInteractionCount")
                                 if isinstance(entity.get("interactionStatistic"), dict) else None,
                                 "location": (entity.get("address") or {}).get("addressLocality")
                                 if isinstance(entity.get("address"), dict) else None}}  # fmt: skip
    m = meta_tags(html)
    if not m.get("og:title"):
        raise WireUnsupported("linkedin returned a login wall; run `wire auth login linkedin.com`")
    return {"title": m.get("og:title"), "body": m.get("og:description", "")}


def parse_voyager_profile(data: dict) -> dict:
    included = data.get("included", [])
    profile = next((i for i in included if i.get("$type", "").endswith(".Profile")), {})
    positions = [i for i in included if i.get("$type", "").endswith(".Position")]
    schools = [i for i in included if i.get("$type", "").endswith(".Education")]
    lines = [profile.get("headline") or "", profile.get("summary") or ""]
    if positions:
        lines.append(
            "## Experience\n"
            + "\n".join(f"- {p.get('title')} · {p.get('companyName')}" for p in positions)
        )
    if schools:
        lines.append("## Education\n" + "\n".join(f"- {s.get('schoolName')}" for s in schools))
    return {"title": f"{profile.get('firstName', '')} {profile.get('lastName', '')}".strip(),
            "body": "\n\n".join(x for x in lines if x), "metadata": {"location": profile.get("locationName")}}  # fmt: skip


class LinkedInChannel(Channel):
    name = "linkedin"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        if jid := job_id(url):
            f = await self.fetch(f"{GUEST}/jobPosting/{jid}", archive=False)
            job = parse_job(f.text)
            if not job["title"]:
                raise WireUnsupported(f"linkedin job {jid} not found or expired")
            return self.doc(f, f"https://www.linkedin.com/jobs/view/{jid}", title=job["title"],
                            author_handle=job["company"], body=job["body"],
                            metadata={"location": job["location"], **job["criteria"]})  # fmt: skip
        segs = [s for s in urlsplit(url).path.split("/") if s]
        jar = await self.ctx.vault.cookies_for("linkedin.com") if self.ctx.vault else {}
        if segs[:1] == ["in"] and len(segs) > 1 and jar.get("JSESSIONID"):
            csrf = jar["JSESSIONID"].strip('"')
            f = await self.fetch(
                f"https://www.linkedin.com/voyager/api/identity/profiles/{segs[1]}/profileView",
                headers={"csrf-token": csrf, "accept": "application/vnd.linkedin.normalized+json+2.1"},
                archive=False, browser=False, cookies=jar,
            )  # fmt: skip
            return self.doc(f, url, **parse_voyager_profile(parse_json(f.content)))
        f = await self.fetch(url, archive=False)
        return self.doc(f, url, **parse_public_page(f.text))

    async def search(self, query, *, limit=5, sort="relevance"):
        f = await self.fetch(
            f"{GUEST}/seeMoreJobPostings/search?keywords={quote(query)}&start=0", archive=False
        )
        return parse_job_search(f.text)[:limit]


CHANNEL = LinkedInChannel
