"""Boss Zhipin job descriptions (heavy anti-bot: local session or the stage-4 browser usually needed)."""

from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown


def parse_job(html: str) -> dict:
    tree = LexborHTMLParser(html)
    desc = tree.css_first(".job-sec-text, .job-detail-section .text")
    if not desc:
        raise WireUnsupported(
            "zhipin served a verification page; run `wire auth login zhipin.com` or install the stealth extra"
        )

    def text(sel):
        n = tree.css_first(sel)
        return n.text(strip=True) if n else None

    tags = [t.text(strip=True) for t in tree.css(".job-tags span, .job-keyword-list li")]
    return {"title": text(".name h1, .job-name"), "author_handle": text(".company-info a[ka], .company-name"),
            "body": html_to_markdown(desc.html),
            "metadata": {"salary": text(".salary, .job-salary"), "city": text(".text-city, .job-area"),
                         "tags": ", ".join(tags) or None}}  # fmt: skip


class BossZhipinChannel(Channel):
    name = "boss_zhipin"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        f = await self.fetch(url, archive=False)
        return self.doc(f, url, **parse_job(f.text))


CHANNEL = BossZhipinChannel
