# Agent Wire documentation

Agent Wire by uncoalesced is a local MCP server that gives AI agents live web pages, web search and platform reads. On PyPI it's `agent-wire-cli`, the shell command is `wire`, and the Claude plugin is `agent-wire`. The [README](README.md) covers the same ground with benchmarks and background; this page is the reference.

## Install

You need [uv](https://docs.astral.sh/uv/).

- **Claude Code plugin:** `/plugin install agent-wire --marketplace uncoalesced/agent-wire`. It starts `uvx agent-wire-cli==<version> serve` and asks for optional keys when you enable it.
- **Any other client:** `uvx agent-wire-cli install --client auto` finds the agent clients on your machine (Claude Code, Claude Desktop, Codex, Cursor, Windsurf, VS Code, Roo, Gemini CLI, Hermes, Antigravity, Peridot), shows what it will write, and asks first. `--client <name>`, `--dry-run`, `--global` and `--project PATH` narrow it down.
- **By hand:** run `uvx agent-wire-cli serve` as a stdio MCP server.

Optional extras: `agent-wire-cli[stealth]` (headless browser for Cloudflare-protected pages and the `wire auth login` window), `agent-wire-cli[browser-cookies]` (import a login from Chrome, Edge or Brave), `agent-wire-cli[local-whisper]` (local podcast transcription).

## Tools

| Tool | What it does |
|---|---|
| `wire_read(urls, query, section, include_comments, max_tokens, force)` | Reads up to 10 URLs. `query` keeps the parts that answer it; `#L10-L50` and `#heading` anchors work. A page read again returns only what changed. |
| `wire_search(query, platforms, limit, sort, auto_expand_top, max_tokens)` | Searches the web or specific platforms and reads the top hits in the same call. |
| `wire_feed(platform, target, sort, limit)` | A subreddit, an `@user` timeline, GitHub trending, an arXiv category, a stock watchlist, an RSS feed. |
| `wire_me(platform, target, limit)` | Your own posts, likes, bookmarks, notifications or stats, through a session you saved. |
| `wire_research(topic, days, ...)` | One cited brief of what people said about a topic in the last N days, an "A vs B" comparison, or what's trending. |
| `wire_cite(text, check_quotes)` | Opens every link in a text and reports dead, archived-only, bot-blocked and soft-404 pages, quotes missing from their page, and `[[UNSOURCED]]` claims. |
| `wire_run(code, max_tokens)` | Code Mode: one sandboxed JavaScript script that calls `wire.read/search/feed/research/cite` and returns only what the script returns. |
| `wire_status(refresh)` | Channels, saved logins, route cache and alert counts. |

Every result starts with a `[WIRE | ...]` line, then any `[WIRE SHIELD | ...]` alert lines, then the page content inside an `[UNTRUSTED_EXTERNAL_DATA]` block. Everything inside that block is quoted material from the web, not instructions.

How an agent should use the tools (when to call each one, citation rules) is in the skill and rule files the installer writes: [`skills/agent-wire/SKILL.md`](skills/agent-wire/SKILL.md), and the Cursor, Windsurf and Hermes files in [`manifests/`](manifests/).

## Logins

Most sites need no login. For your own account (`wire_me`) and login-only sites:

- `wire auth login <site>` opens a login window (needs the `[stealth]` extra) and saves that site's cookies to an encrypted vault.
- `wire auth login <site> --from-browser` copies that one site's cookies from a browser you're signed in to. Firefox works out of the box; Chrome, Edge and Brave need `[browser-cookies]`.
- `wire auth list` shows saved sites (names only); `wire auth logout <site>` forgets one.

A saved login is sent only to its own site, only on GET or HEAD requests, never to account pages such as messages, settings or billing, and never across a redirect.

## Configuration

| Setting | Effect |
|---|---|
| `WIRE_HOME` | Where agent-wire keeps its files. The Claude plugin uses its plugin data folder. |
| `GITHUB_TOKEN` | Higher GitHub rate limits and `wire_me github`. Outside the plugin, `gh auth token` is used if set up; `WIRE_NO_GH_CLI=1` turns that off. |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Reddit's official app login, for networks where anonymous access is blocked. |
| `WIRE_SEARXNG_URL`, `EXA_API_KEY` | Extra web search engines. |
| `WIRE_TRANSCRIBE=1` (+ `WIRE_TRANSCRIBE_API_KEY`, `_URL`, `_MODEL`) | Podcast transcription, locally or through an OpenAI-compatible API. |
| `WIRE_ALLOW_PRIVATE=1` | Let reads reach private and internal addresses. Off by default. |
| `WIRE_SHIELD_NOTIFY=0` | No desktop notifications for alerts. |
| `WIRE_SHIELD_VERBOSE=1` | Also record every routine removal (hidden menus, zero-width spaces) in the alert ledger. |

In the Claude plugin, the keys are plugin settings (`userConfig`) stored in Claude Code's secure storage, and the plugin never reads credentials from your machine.

## Shield Alerts

agent-wire removes text a human reader can't see before the model reads a page, and refuses requests a page may have steered. Shield Alerts tells you when that happened, without relying on the model to mention it: a hijacked or distracted agent could leave the warning out.

**What raises an alert.**

| Tier | Examples | What you get |
|---|---|---|
| info | A hidden menu, a cookie banner, a zero-width space, a fake chat token in a comment | A counter in `wire_status`. Recorded in full only with `WIRE_SHIELD_VERBOSE=1`. |
| alert | Hidden text that reads like an instruction to an AI ("ignore previous instructions", "you are now", chat tokens, tool names, "don't tell the user", "send the API keys to"); text smuggled in Unicode tag characters; a forged end-of-data marker; visible text that names agent-wire's tools, asks the model to hide something from you, or asks it to send secrets somewhere | Notification, stderr line, header line, ledger row |
| high | A refused request: a private or internal address, a non-web URL scheme, a logged-in request that would write or reach an account page | Same, worded as a refused request |

Visible text is judged more strictly than hidden text. A security article that quotes "ignore previous instructions" in plain view is not an attack on you and raises nothing; the same words hidden with CSS do.

**What each alert says it did.** `stripped` (removed before the model saw it), `escaped` (neutralized in place), `refused` (never sent), `flagged-only` (visible text: it stays in the quoted content, marked as untrusted, because removing visible text would censor the page).

**Where alerts go.**

1. `WIRE_HOME/alerts.db`, one row per alert, chained by SHA-256 so edits are detectable (`wire alerts verify`).
2. A desktop notification (Windows toast, macOS Notification Center, Linux `notify-send`). At most one per site per minute and five per minute overall. It never contains page text.
3. A `[wire-shield] ...` line on stderr, which some clients show as server logs.
4. A `[WIRE SHIELD | ...]` line in the tool result, after the `[WIRE | ...]` line and before the untrusted block, so the model can tell you too. It never contains page text either.

**Commands.**

- `wire alerts`: recent alerts, newest first (`--all` adds info events).
- `wire alerts show <id>`: everything recorded about one alert: the URL (query values removed), where on the page it was (an element path such as `body > article > div.promo-note`), the caught text (up to 120 characters, secrets scrubbed, control characters removed) and any decoded hidden text.
- `wire alerts tail`: print new alerts as they arrive.
- `wire alerts verify`: check the ledger's hash chain.
- `wire alerts demo`: run the shield on a bundled page with three hidden attacks and one honest quote, offline, and show what the model receives.

**Limits.** Locations are element paths, not line numbers: the HTML parser doesn't keep source positions. Only `<style>` blocks inside the page are applied, and only simple `.class`, `#id` and `tag.class` selectors outside `@media` and similar blocks; the common hiding classes (`sr-only`, `visually-hidden`, `hidden`, `d-none`, `invisible`) are recognised by name, unless a responsive class such as `md:block` or `d-lg-block` shows the element again. Detection rules are patterns, so a novel phrasing in visible text may not be flagged; hidden text is removed either way.

## Citations

Write `[[UNSOURCED]]` after a claim with no source, and run `wire_cite` on an answer before sending it. It reports `OK`, `DEAD`, `ARCHIVED` (with the Wayback copy), `SOFT_404`, `BLOCKED`, `QUOTE_MISSING`, and links agent-wire never returned this session.

## Code Mode

`wire serve --code-mode` exposes only `wire_run` and `wire_status`. Scripts run in a QuickJS sandbox with no filesystem, network, timers or modules, and with memory, CPU, wall-clock and call limits. They can't reach `wire_me`.

## Transports

`wire serve` uses stdio. `--transport http` or `sse` binds to `127.0.0.1` only and has no authentication; don't expose it to other machines.

## Policies and help

[Privacy Policy](PRIVACY.md) · [Terms of Use](TERMS.md) · [Support](SUPPORT.md) · [Security](SECURITY.md) · [License](LICENSE)
