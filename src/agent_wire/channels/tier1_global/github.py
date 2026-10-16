"""GitHub: repo overview, blobs with #L slicing, issues/PRs, gists, users, search, trending."""

import asyncio
import os
import shutil
import subprocess
from functools import cache
from urllib.parse import quote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from agent_wire.channels.base import Channel, WireUnsupported, compact_count, listing, ts
from agent_wire.compression.anchor_slicer import parse_line_anchor, slice_lines, slice_section
from agent_wire.models import WireComment, WireSearchHit

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
_LANG = {"py": "python", "js": "javascript", "ts": "typescript", "rs": "rust", "go": "go",
         "rb": "ruby", "java": "java", "kt": "kotlin", "c": "c", "h": "c", "cpp": "cpp",
         "cs": "csharp", "sh": "bash", "md": "markdown", "toml": "toml", "yml": "yaml",
         "yaml": "yaml", "json": "json", "tsx": "tsx", "jsx": "jsx", "swift": "swift"}  # fmt: skip
_SKIP_PATHS = frozenset({"orgs", "settings", "notifications", "login", "marketplace", "topics"})


@cache
def github_token() -> str | None:
    if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return token
    if gh := shutil.which("gh"):
        try:
            out = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=3, check=False
            )
            return out.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return None


class GitHubChannel(Channel):
    name = "github"

    def _headers(self, raw: bool = False) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token := github_token():
            h["Authorization"] = f"Bearer {token}"
        return h

    async def api(self, path: str):
        data, f = await self.fetch_json(f"{API}{path}", headers=self._headers())
        return data, f

    async def raw(self, url: str) -> str:
        f = await self.fetch(url, headers=self._headers(raw=True), archive=False, browser=False)
        return f.text

    # ---- read ------------------------------------------------------------------------------
    async def read(self, url, *, query=None, section=None, include_comments=True):
        parts = urlsplit(url)
        host, segs = parts.hostname or "", [s for s in parts.path.split("/") if s]
        anchor = section or parts.fragment or None
        if host.endswith("githubusercontent.com"):
            return await self._blob_raw(url, segs[0], segs[1], "/".join(segs[3:]), url)
        if host == "gist.github.com" and len(segs) >= 2:
            return await self._gist(url, segs[-1])
        if not segs or segs[0] in _SKIP_PATHS:
            return await self.ctx.channel("web").read(url, query=query, section=section)
        if len(segs) == 1:
            return await self._user(url, segs[0])
        owner, repo = segs[0], segs[1].removesuffix(".git")
        kind = segs[2] if len(segs) > 2 else ""
        if not kind:
            return await self._repo(url, owner, repo, anchor)
        if kind == "blob" and len(segs) > 4:
            raw = f"{RAW}/{owner}/{repo}/{segs[3]}/{'/'.join(segs[4:])}"
            return await self._blob_raw(raw, owner, repo, "/".join(segs[4:]), url)
        if kind == "tree" and len(segs) > 3:
            return await self._tree(url, owner, repo, segs[3], "/".join(segs[4:]))
        if kind in ("issues", "pull") and len(segs) > 3 and segs[3].isdigit():
            return await self._issue(url, owner, repo, int(segs[3]), include_comments)
        if kind == "releases":
            return await self._releases(url, owner, repo)
        if kind == "commit" and len(segs) > 3:
            return await self._commit(url, owner, repo, segs[3])
        return await self.ctx.channel("web").read(url, query=query, section=section)

    async def _repo(self, url, owner, repo, anchor):
        (meta, f), (tree, _), readme = await asyncio.gather(
            self.api(f"/repos/{owner}/{repo}"),
            self.api(f"/repos/{owner}/{repo}/contents"),
            self._readme(owner, repo),
        )
        files = " ".join(
            f"`{e['name']}/`" if e.get("type") == "dir" else f"`{e['name']}`"
            for e in (tree if isinstance(tree, list) else [])
        )
        if anchor and readme:
            readme = slice_section(readme, anchor) or readme
        stats = {
            "stars": meta.get("stargazers_count"), "forks": meta.get("forks_count"),
            "language": meta.get("language"), "license": (meta.get("license") or {}).get("spdx_id"),
            "open_issues": meta.get("open_issues_count"),
            "topics": ", ".join(meta.get("topics") or []) or None,
            "pushed": (meta.get("pushed_at") or "")[:10] or None,
        }  # fmt: skip
        body = "\n\n".join(
            p for p in (meta.get("description"), f"## Files\n{files}" if files else "",
                        f"## README\n{readme}" if readme else "") if p
        )  # fmt: skip
        return self.doc(f, url, title=meta.get("full_name", f"{owner}/{repo}"),
                        author_handle=owner, body=body, metadata=stats)  # fmt: skip

    async def _readme(self, owner, repo) -> str:
        try:
            f = await self.fetch(f"{API}/repos/{owner}/{repo}/readme",
                                 headers=self._headers(raw=True), archive=False, browser=False)  # fmt: skip
            return f.text
        except Exception:  # noqa: BLE001 - a repo without a README is still a repo
            return ""

    async def _blob_raw(self, raw_url, owner, repo, path, url):
        f = await self.fetch(raw_url, headers=self._headers(raw=True), archive=False, browser=False)
        text = f.text
        lines = parse_line_anchor(url)
        if lines:
            text = slice_lines(text, *lines)
        lang = _LANG.get(path.rsplit(".", 1)[-1].lower(), "")
        body = text if lang == "markdown" and not lines else f"```{lang}\n{text}\n```"
        meta = {"lines": f"{lines[0]}-{lines[1]}"} if lines else {}
        return self.doc(f, url, title=f"{owner}/{repo}: {path}", body=body, metadata=meta)

    async def _tree(self, url, owner, repo, ref, path):
        data, f = await self.api(f"/repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(ref)}")
        entries = data if isinstance(data, list) else [data]
        body = "\n".join(
            f"- `{e['name']}{'/' if e.get('type') == 'dir' else ''}`"
            + (f" ({e['size']} B)" if e.get("type") == "file" else "")
            for e in entries
        )
        return self.doc(f, url, title=f"{owner}/{repo}/{path or ''} @ {ref}", body=body)

    async def _issue(self, url, owner, repo, num, include_comments):
        issue, f = await self.api(f"/repos/{owner}/{repo}/issues/{num}")
        meta = {
            "state": issue.get("state"),
            "labels": ", ".join(lb["name"] for lb in issue.get("labels", [])) or None,
            "comments": issue.get("comments"),
        }
        if "pull_request" in issue:
            pr, _ = await self.api(f"/repos/{owner}/{repo}/pulls/{num}")
            meta.update(merged=pr.get("merged"), additions=pr.get("additions"),
                        deletions=pr.get("deletions"), files=pr.get("changed_files"))  # fmt: skip
        comments = []
        if include_comments and issue.get("comments"):
            raw, _ = await self.api(f"/repos/{owner}/{repo}/issues/{num}/comments?per_page=100")
            author = (issue.get("user") or {}).get("login")
            comments = [
                WireComment(
                    id=str(c["id"]), body=c.get("body") or "",
                    author_handle=(c.get("user") or {}).get("login"),
                    timestamp=ts(c.get("created_at")),
                    score=(c.get("reactions") or {}).get("total_count", 0),
                    is_op=(c.get("user") or {}).get("login") == author,
                )
                for c in raw
            ]  # fmt: skip
        return self.doc(
            f, url, title=f"#{num} {issue.get('title', '')}",
            author_handle=(issue.get("user") or {}).get("login"),
            timestamp=ts(issue.get("created_at")), body=issue.get("body") or "",
            score=(issue.get("reactions") or {}).get("total_count"),
            comments=comments, metadata=meta,
        )  # fmt: skip

    async def _releases(self, url, owner, repo):
        data, f = await self.api(f"/repos/{owner}/{repo}/releases?per_page=5")
        body = "\n\n".join(
            f"## {r.get('name') or r.get('tag_name')} ({(r.get('published_at') or '')[:10]})\n"
            f"{r.get('body') or ''}"
            for r in data
        )
        return self.doc(f, url, title=f"{owner}/{repo} releases", body=body)

    async def _commit(self, url, owner, repo, sha):
        c, f = await self.api(f"/repos/{owner}/{repo}/commits/{sha}")
        files = "\n".join(
            f"- `{x['filename']}` +{x.get('additions', 0)} -{x.get('deletions', 0)}"
            for x in c.get("files", [])[:50]
        )
        stats = c.get("stats") or {}
        return self.doc(
            f, url, title=(c.get("commit", {}).get("message") or sha).split("\n")[0],
            author_handle=(c.get("author") or {}).get("login"),
            timestamp=ts(c.get("commit", {}).get("author", {}).get("date")),
            body=f"{c.get('commit', {}).get('message', '')}\n\n## Files\n{files}",
            metadata={"additions": stats.get("additions"), "deletions": stats.get("deletions")},
        )  # fmt: skip

    async def _gist(self, url, gist_id):
        g, f = await self.api(f"/gists/{gist_id}")
        body = "\n\n".join(
            f"## {name}\n```{_LANG.get(name.rsplit('.', 1)[-1].lower(), '')}\n{fl.get('content', '')}\n```"
            for name, fl in (g.get("files") or {}).items()
        )
        return self.doc(f, url, title=g.get("description") or gist_id,
                        author_handle=(g.get("owner") or {}).get("login"), body=body)  # fmt: skip

    async def _user(self, url, login):
        (u, f), (repos, _) = await asyncio.gather(
            self.api(f"/users/{login}"), self.api(f"/users/{login}/repos?sort=updated&per_page=15")
        )
        body = (u.get("bio") or "") + "\n\n## Repositories\n" + _repo_list(repos)
        return self.doc(f, url, title=u.get("name") or login, author_handle=login, body=body,
                        metadata={"followers": u.get("followers"), "public_repos": u.get("public_repos")})  # fmt: skip

    # ---- search / feed / me ----------------------------------------------------------------
    async def search(self, query, *, limit=5, sort="relevance"):
        order = "&sort=stars" if sort in ("top", "stars") else ""
        data, _ = await self.api(f"/search/repositories?q={quote(query)}&per_page={limit}{order}")
        return [
            WireSearchHit(url=r["html_url"], title=r["full_name"], snippet=r.get("description") or "",
                          source="github", rank_score=float(r.get("stargazers_count", 0)))
            for r in data.get("items", [])[:limit]
        ]  # fmt: skip

    async def feed(self, target, *, sort="hot", limit=10):
        t = target.strip().strip("/").removeprefix("https://github.com/")
        if t.startswith("trending"):
            url = f"https://github.com/{t}"
            f = await self.fetch(url, archive=False)
            items = parse_trending(f.text)[:limit]
            return self.doc(f, url, title=f"GitHub {t}", body=listing(items))
        if "/" in t:
            data, f = await self.api(f"/repos/{t}/issues?state=open&sort=updated&per_page={limit}")
            items = [{"title": f"#{i['number']} {i['title']}", "url": i["html_url"],
                      "meta": [i.get("state"), f"{i.get('comments', 0)} comments"]} for i in data]  # fmt: skip
            return self.doc(
                f, f"https://github.com/{t}/issues", title=f"{t} issues", body=listing(items)
            )
        login = t.lstrip("@")
        repos, f = await self.api(f"/users/{login}/repos?sort=updated&per_page={limit}")
        return self.doc(
            f, f"https://github.com/{login}", title=f"@{login} repos", body=_repo_list(repos)
        )

    async def me(self, target="latest_posts", *, limit=5):
        if not github_token():
            raise WireUnsupported("wire_me github needs GITHUB_TOKEN or `gh auth login`")
        paths = {
            "notifications": f"/notifications?per_page={limit}",
            "latest_posts": f"/user/repos?sort=updated&per_page={limit}",
            "likes": f"/user/starred?per_page={limit}",
            "bookmarks": f"/user/starred?per_page={limit}",
            "prs": f"/search/issues?q={quote('is:pr author:@me is:open')}&per_page={limit}",
            "stats": "/user",
        }
        if target not in paths:
            raise WireUnsupported(f"github targets: {', '.join(paths)}")
        data, f = await self.api(paths[target])
        if target == "stats":
            body = listing([{"title": data.get("login"), "url": data.get("html_url"),
                             "meta": [f"{data.get('followers')} followers",
                                      f"{data.get('public_repos')} public repos"]}])  # fmt: skip
        elif target == "notifications":
            body = listing([{"title": n["subject"]["title"],
                             "url": f"https://github.com/{n['repository']['full_name']}",
                             "meta": [n["subject"]["type"], n.get("reason"), (n.get("updated_at") or "")[:10]]}
                            for n in data])  # fmt: skip
        elif target == "prs":
            body = listing([{"title": i["title"], "url": i["html_url"], "meta": [i.get("state")]}
                            for i in data.get("items", [])])  # fmt: skip
        else:
            body = _repo_list(data)
        return self.doc(f, f"https://github.com/{target}", title=f"my github {target}", body=body)


def _repo_list(repos) -> str:
    return listing(
        [
            {
                "title": r.get("full_name") or r.get("name"),
                "url": r.get("html_url"),
                "meta": [f"★{r.get('stargazers_count', 0)}", r.get("language")],
                "snippet": r.get("description") or "",
            }
            for r in (repos if isinstance(repos, list) else [])
        ]
    )


def parse_trending(html: str) -> list[dict]:
    items = []
    for row in LexborHTMLParser(html).css("article.Box-row"):
        a = row.css_first("h2 a")
        if not a:
            continue
        path = (a.attributes.get("href") or "").strip()
        desc = row.css_first("p")
        stars = row.css_first('a[href$="/stargazers"]')
        today = row.css_first("span.d-inline-block.float-sm-right")
        lang = row.css_first('[itemprop="programmingLanguage"]')
        items.append(
            {
                "title": path.strip("/"),
                "url": f"https://github.com{path}",
                "meta": [
                    f"★{compact_count(stars.text(strip=True))}" if stars else None,
                    lang.text(strip=True) if lang else None,
                    today.text(strip=True) if today else None,
                ],
                "snippet": desc.text(strip=True) if desc else "",
            }
        )
    return items


CHANNEL = GitHubChannel
