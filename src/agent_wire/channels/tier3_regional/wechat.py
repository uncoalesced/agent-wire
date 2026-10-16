"""WeChat official-account articles (mp.weixin.qq.com) and Sogou WeChat search."""

import re
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, html_to_markdown, ts
from agent_wire.models import WireSearchHit

WECHAT_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/124.0 Mobile Safari/537.36 MicroMessenger/8.0.49")  # fmt: skip


def parse_article(html: str) -> dict:
    tree = LexborHTMLParser(html)
    content = tree.css_first("#js_content")
    if not content:
        raise WireUnsupported("wechat article body missing (deleted, or a verification page)")
    # WeChat ships the body as visibility:hidden and reveals it with JS; the shield would drop it,
    # so convert the children, not the hidden wrapper.
    inner = "".join(child.html or "" for child in content.iter(include_text=True))
    title = tree.css_first("#activity-name, h1.rich_media_title")
    author = tree.css_first("#js_name")
    ct = re.search(r'\bvar ct\s*=\s*"(\d+)"', html) or re.search(
        r"create_time\s*[:=]\s*['\"]?(\d{9,})", html
    )
    return {"title": title.text(strip=True) if title else None,
            "author_handle": author.text(strip=True) if author else None,
            "timestamp": ts(ct[1]) if ct else None, "body": html_to_markdown(inner, links=False)}  # fmt: skip


def parse_sogou(html: str) -> list[WireSearchHit]:
    hits = []
    for li in LexborHTMLParser(html).css("ul.news-list li"):
        a = li.css_first("h3 a")
        if not a:
            continue
        href = a.attributes.get("href") or ""
        snippet = li.css_first("p.txt-info")
        account = li.css_first(".s-p .all-time-y2, .s-p a.account")
        hits.append(WireSearchHit(
            url=href if href.startswith("http") else f"https://weixin.sogou.com{href}", title=a.text(strip=True),
            snippet=" · ".join(x for x in (account.text(strip=True) if account else "",
                                           snippet.text(strip=True) if snippet else "") if x),
            source="wechat"))  # fmt: skip
    return hits


class WeChatChannel(Channel):
    name = "wechat"

    async def read(self, url, *, query=None, section=None, include_comments=True):
        f = await self.fetch(url, headers={"User-Agent": WECHAT_UA}, archive=False)
        return self.doc(f, url, **parse_article(f.text))

    async def search(self, query, *, limit=5, sort="relevance"):
        f = await self.fetch(
            f"https://weixin.sogou.com/weixin?type=2&query={quote(query)}", archive=False
        )
        hits = parse_sogou(f.text)
        if not hits and "antispider" in f.url:
            raise WireUnsupported("Sogou WeChat search showed a CAPTCHA; try again later")
        return hits[:limit]


CHANNEL = WeChatChannel
