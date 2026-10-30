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

## Known limitations

- **Secret redaction.** Output scrubbing removes bearer tokens, session cookies, private IPs, your saved vault values and well-known prefixed API keys (GitHub, OpenAI, Anthropic, AWS, Slack, Google, GitLab, Stripe, Hugging Face, npm). Keys with no recognizable prefix that appear in public page text are not caught.
