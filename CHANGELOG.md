# Changelog

All notable changes to agent-wire (`agent-wire-cli` on PyPI, the `agent-wire` Claude plugin).

## 0.2.5 (2026-11-15)

### Added
- **`wire selftest`.** Checks every subsystem in one offline run, and a guard fails the run on any network attempt. It covers:
  - all 31 channels, on canned responses
  - the inbound and outbound firewalls, and the SSRF guard
  - Shield Alerts and the alert ledger
  - compression, the delta buffer and the route ledger
  - the cookie vault and Code Mode
  - research and `wire_cite`
  - the 11 client installers
  - the MCP server, in-process and over stdio

  `--live` adds one real request per channel (Reddit with no API key) and counts shield false positives on the 16 README benchmark pages. `--only` and `--json` narrow and format the run. CI and the release workflow run the offline check.
- **Shield Alerts.** Stripped hidden prompt injections, forged delimiters and refused outbound requests are reported to you out of band: a desktop notification, a stderr line, and a trusted `[WIRE SHIELD | ...]` line ahead of the quoted content. Alerts are kept in a hash-chained `alerts.db`. New commands: `wire alerts`, `show`, `tail`, `verify`, `demo`.
- **Root policy documents:** `PRIVACY.md`, `TERMS.md`, `DOCUMENTATION.md`, `SUPPORT.md`.

### Changed
- **The Claude plugin now lives in `plugin/`.** It launches the published package with `uvx agent-wire-cli==0.2.5 serve`.
  - Optional credentials come from the plugin's `userConfig`, marked sensitive. Each one is sent only to its own service.
  - The plugin never shells out to `gh`.
- **`WIRE_HOME` prefers `CLAUDE_PLUGIN_DATA`,** so plugin updates keep your data.
- **`rookiepy` is now the `[browser-cookies]` extra,** and `wire_status` no longer looks at browser profiles.
- **Server tool descriptions describe the tools only.** When-to-use guidance stays in the skill, whose description no longer tells the model to act "automatically".
- **Setting a transcription API key turns transcription on.** This is what the plugin's `transcribe_api_key` option implied all along. `WIRE_TRANSCRIBE=0` still turns it off.
- **Token counting falls back to an approximation** when the tokenizer file can't be downloaded on first run, so reads keep working offline.
- **The lock moves dnspython to 2.9.2 and opentelemetry-api to 1.45.2** (CVE-2026-128089, CVE-2026-126360; both are transitive dependencies through fastmcp).
- **`joserfc>=1.8.0` is required** (GHSA-jpxq-7h8p-2q76, GHSA-9pg5-8c7x-5hfx). agent-wire itself never parses JWS or JWE.
- **The HTTP/SSE transport binds `127.0.0.1` explicitly.**

### Fixed
- **Hidden-text stripping now catches:**
  - text hidden by the page's own stylesheet (`.x{display:none}`, `#id{visibility:hidden}`)
  - text hidden by the `sr-only`, `visually-hidden`, `hidden` and `d-none` classes
  - text hidden with `clip`, `clip-path`, a transparent color, or off-screen `top`/`right`/`margin`
- **Invisible characters written as HTML entities** (`&#xE0041;`, `&#8203;`) are now removed after parsing.

### Measured
- **Attack corpus:** 12 of 12 hiding techniques detected and stripped.
- **Benign corpus:** 0 alerts across 9 cases.
- **`wire selftest --live`, 2026-11-15:**
  - **Benchmark pages:** none raised an alert, over two runs (16 of 16 read on the first; on the second, the Cloudflare challenge page needed the stealth browser, so 15 were read).
  - **Channels:** 26 of 31 answered with no keys or logins, including Reddit.
  - **Skipped (5):**
    - Zhihu and Boss Zhipin need a login or the stealth browser.
    - TikTok's oEmbed refused the probe.
    - Xiaohongshu and Xiaoyuzhou have no stable public URL to probe.

## Earlier releases

See the [GitHub releases page](https://github.com/uncoalesced/agent-wire/releases).
