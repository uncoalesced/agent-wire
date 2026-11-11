# Security Policy

## Supported versions

Only the latest release on PyPI (`agent-wire-cli`) gets security fixes. Please upgrade before you report.

| Version | Supported |
| ------- | --------- |
| 0.2.x (latest) | Yes |
| < 0.2 | No |

## Reporting a vulnerability

Please don't open a public issue. Report it privately through [GitHub's vulnerability reporting](https://github.com/uncoalesced/agent-wire/security/advisories/new) instead.

Include the version (`wire version`), your OS and Python version, the tool or command you used, and the smallest input that reproduces the problem. That could be a URL, a page, or a Code Mode script. For prompt-injection reports, attach or describe the page content that got through.

You'll get an answer within 7 days. Confirmed issues are fixed in a patch release and credited in the advisory, unless you'd rather stay anonymous. Please hold off on public disclosure until the fix ships, or until 90 days have passed, whichever comes first.

## What's in scope

agent-wire sits between an AI agent and the open web, so it treats two things as hostile: the pages it fetches, and the agent's own requests, which a page may have steered. These are vulnerabilities:

- **Leaks of your identity.** A saved cookie or login sent to a different site, sent on a non-read request, or carried across a redirect. Vault contents, tokens or private IPs that reach the model's output.
- **Prompt injection getting through.** Hidden page text (CSS-hidden, zero-size, `aria-hidden`, HTML comments, Unicode tag, zero-width or bidi characters) surviving extraction. Text that closes the `UNTRUSTED_EXTERNAL_DATA` block early, or escapes it.
- **Account actions.** A cookie-bearing request reaching account paths such as `/messages`, `/settings`, `/billing` or `/password`, or using any method other than GET or HEAD.
- **Code Mode sandbox escapes.** A `wire_run` script that reaches the filesystem, network, timers, modules or Python. A script that gets past the memory, CPU, wall-clock or call limits, crashes or hangs the server, or forges results or citation provenance.
- **Vault weaknesses.** Problems with the AES-GCM vault or how its key is protected (Windows DPAPI, or a 0600 key file on macOS and Linux).

## What's out of scope

- Content a site really published. agent-wire quotes pages; it doesn't vouch for them. Results are untrusted by design.
- A model that follows instructions found inside an `UNTRUSTED_EXTERNAL_DATA` block. The block is a signal to the model, not a guarantee. A bypass of the shield itself is in scope.
- Getting blocked, rate-limited or challenged by a site.
- Problems that need someone who can already edit your `WIRE_HOME`, your agent client config or your environment variables.

## Supply chain

Dependency problems are in scope too: a vulnerable or malicious package in `uv.lock`, a release file that doesn't match this repository, or a workflow that could leak a secret or publish something we didn't build.

- **Releases.** A pushed `v*` tag runs `.github/workflows/release.yml`, which builds the wheel and sdist, runs the tests and uploads to PyPI through Trusted Publishing. No PyPI token is stored anywhere. Each file gets a PEP 740 attestation naming this repository and workflow. Check one with `uvx --from pypi-attestations pypi-attestations verify pypi --repository https://github.com/uncoalesced/agent-wire pypi:<file name>`.
- **Dependencies.** `uv.lock` pins every package with sha256 hashes, and CI installs with `uv sync --locked`. Every locked package ships wheels, so installing runs no package code. There is no npm or Node dependency. Routine upgrades come through Dependabot, which waits 7 days after a release; security updates come straight away.
- **Checks on every push and pull request.** `pip-audit` over the full locked set including extras, bandit, zizmor on the workflows, CodeQL, and dependency review on pull requests. Actions are pinned to commit SHAs.
- **Last manual scan, 2026-11-11.** `pip-audit` over the locked set and over a fresh resolve from PyPI (what `uvx agent-wire-cli` installs): no known vulnerabilities.
- **joserfc advisories.** `joserfc` arrives through `fastmcp` and `authlib`. Two advisories affect older versions: GHSA-jpxq-7h8p-2q76 (CVE-2026-119283, high, CPU exhaustion through an unbounded JWE `recipients` array, fixed in 1.7.5) and GHSA-9pg5-8c7x-5hfx (CVE-2026-119284, moderate, the compact JWS size guard allocates before it rejects, fixed in 1.7.6). agent-wire never parses a JWS or JWE: no code in `src/` uses joserfc or authlib, and `wire serve` configures no auth on any transport. The lock moved to 1.8.0 on 2026-11-04. Since 0.2.5, `pyproject.toml` also requires `joserfc>=1.8.0`, because `uvx` and `pip` resolve from package metadata, not from `uv.lock`.
- **2026-11-04 scan.** CodeQL's substring host-check alerts in the channel routers were fixed; the ones left in tests are assertions on test data.
- **HTTP transport.** `wire serve --transport http|sse` has no authentication and binds to `127.0.0.1` only. Don't put it behind a proxy that exposes it to other machines.

## Fixed in 0.2.5

- **Text hidden by a class or stylesheet.** Earlier versions checked only an element's inline `style`. Text hidden through the page's own `<style>` rules (`.x{display:none}`, `#id{visibility:hidden}`) or the standard `sr-only` / `visually-hidden` / `hidden` / `d-none` classes reached the model. Clipped (`clip`, `clip-path`), transparent and `top`/`right`/`margin` off-screen text was also missed.
- **Invisible characters written as HTML entities.** Unicode tag, zero-width and bidi characters were stripped before parsing, so `&#xE0041;` or `&#8203;` survived as real characters after the parser decoded them. `clean_html` now strips them again after parsing.
- **Shield Alerts.** Stripped injections, forged delimiters and refused outbound requests are now reported to the user out of band (see README, "Shield Alerts").

## Known limitations

- **Visible text is flagged, not removed.** Instructions written in plain view stay in the quoted content; when they name agent-wire's tools or tell the model to hide something from the user, an alert says so. A model that follows them anyway is out of scope, as above.
- **Stylesheet coverage.** Only `<style>` blocks in the page, and only simple `.class`, `#id` and `tag.class` selectors outside `@media`/`@supports` blocks, are applied. External stylesheets and complex selectors are not evaluated; the common hiding classes are recognised by name instead.
- **Alert ledger.** `alerts.db` is chained by hash, so edits are detectable (`wire alerts verify`), but someone who can write to `WIRE_HOME` can rebuild the chain. That is out of scope, as above.

- **Secret redaction.** Output scrubbing removes bearer tokens, session cookies, private IPs, your saved vault values and well-known prefixed API keys (GitHub, OpenAI, Anthropic, AWS, Slack, Google, GitLab, Stripe, Hugging Face, npm). Keys with no recognizable prefix that appear in public page text are not caught.
