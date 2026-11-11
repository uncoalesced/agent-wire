# Privacy Policy

Agent Wire by uncoalesced (`agent-wire-cli` on PyPI, the `agent-wire` Claude plugin). Last updated 2026-11-11.

agent-wire is software that runs on your own machine. There is no agent-wire server, no account and no sign-up. It sends no analytics and no telemetry. Nothing it reads or stores is uploaded to uncoalesced.

## What it stores, and where

Everything lives in one local folder, `WIRE_HOME`. For the Claude plugin, that's the plugin's data folder that Claude Code gives it. Otherwise it's a `.wire/` folder inside the agent client that started the server, falling back to `~/.config/agent-wire` (macOS, Linux) or `%APPDATA%\agent-wire` (Windows). You can set `WIRE_HOME` to choose.

| What | File | How long |
|---|---|---|
| Pages and results you asked for | Memory only (a session buffer) | Until the server stops |
| Which fetch stage worked for each site ("route memory") | `cache.db` | Ignored after 24 hours |
| Saved logins (`wire auth login <site>`) | `vault.enc`, encrypted with AES-GCM; the key is protected by Windows DPAPI, or kept in a 0600 file (`vault.key`) on macOS and Linux | Until you run `wire auth logout <site>` |
| Research runs, only if you pass `store=True` or use `wire watch` | `research.db` | Until you delete them |
| Shield alerts: what agent-wire stripped, escaped or refused, where, and up to 120 characters of the page text it caught (with secrets scrubbed and URL query values removed) | `alerts.db` | Until you delete it |
| Browser profile for the optional stage 4 browser (`[stealth]` extra) | `browser-profile/` | Until you delete it |
| Tokenizer data downloaded on first use | `tiktoken/` | Until you delete it |

Deleting `WIRE_HOME` removes everything agent-wire stored.

A login window (`wire auth login <site>`) uses a temporary browser profile that is deleted as soon as the cookies are saved to the vault. `wire auth login <site> --from-browser` reads that one site's cookies from a browser on your machine, only when you run the command yourself. Chrome, Edge and Brave need the optional `agent-wire-cli[browser-cookies]` extra. The MCP server never reads browser profiles.

## Who receives requests

agent-wire only sends requests needed to answer what you or your agent asked:

- **The sites you ask it to read or search**: the web page itself, or platforms such as GitHub, Reddit, Hacker News, YouTube, X, Wikipedia, Stack Exchange, arXiv, Bluesky, Mastodon, Polymarket, Bilibili and the other channels listed in the README.
- **Public services it uses to search and fall back**: DuckDuckGo and Brave Search for web search, Google News and Techmeme for news, the Wayback Machine (archive.org), PullPush and Arctic Shift (Reddit archives), OpenAlex and Semantic Scholar (papers), and fxtwitter, vxtwitter and X's syndication endpoint for single X posts.
- **Services you configure yourself**: Exa (with your Exa key), your own SearXNG instance, an OpenAI-compatible transcription API (only when you set `WIRE_TRANSCRIBE=1`; the audio goes to it), and webhooks you add with `wire watch add`.
- **The tokenizer download**: on first use, `tiktoken` downloads its encoding file from OpenAI's public file host. Nothing about you is sent with it.

Each of those services handles requests under its own privacy policy.

## Your credentials

Optional keys (GitHub token, Reddit app ID and secret, Exa key, transcription key) are read only from your environment, or in the Claude plugin from settings you choose to enter, which Claude Code keeps in secure storage. Each one goes only to its own service. A saved login cookie is sent only to the site it belongs to, only on GET or HEAD requests, never to account pages such as messages, settings or billing, and never across a redirect to another site.

## What your AI model sees

Page text after agent-wire removes hidden content and scrubs tokens, cookies, private IP addresses, well-known API key formats, your saved vault values and, on logged-in pages, email addresses. Your cookies and identity are not passed to the model. Whatever your model provider does with the text it receives is covered by that provider's policy, not this one.

## Children

agent-wire is a developer tool and is not directed at children.

## Changes

Changes to this policy are made in this file and recorded in the repository history.

## Contact

mail@uncoalesced.com, or an issue at [github.com/uncoalesced/agent-wire/issues](https://github.com/uncoalesced/agent-wire/issues). Report security problems privately as described in [SECURITY.md](SECURITY.md).
