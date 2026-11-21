# Running agent-wire for a team

This is for operators: security teams, platform teams and anyone who runs agent-wire for other people or for unattended agents. It covers what protects a deployment, what you can configure, and what agent-wire does not do. For reporting a vulnerability see [SECURITY.md](SECURITY.md). No certification (SOC 2, ISO 27001) is claimed; the controls below are building blocks for yours.

## Threat model

agent-wire sits between an AI agent and the open web. It treats these as hostile or fallible:

| Risk | Control |
| --- | --- |
| A page tries to steer the agent (prompt injection) | Inbound shield: hidden text is stripped, instructions in plain view are flagged, quoted content is fenced as `[UNTRUSTED_EXTERNAL_DATA]`. Alerts go to an out-of-band ledger. |
| The agent, or a page steering it, reaches your network | SSRF guard on every request and redirect hop: loopback, LAN, link-local and cloud-metadata addresses are refused. Remote mode cannot turn that off. |
| The agent leaks your identity | Remote mode has no `wire_me`, no vault, no cookies. Outbound text is scrubbed for tokens, cookies and private IPs. |
| An agent runs away (loops, bulk fetching) | Call and byte budgets, a repeat-call stop, a kill switch. |
| Nobody can say what an agent did | A signed, hash-chained audit log. |
| A stranger reaches the server | Bearer token or OAuth, `Host` and `Origin` checks, no non-loopback listener without credentials. |

Not covered: a model that obeys instructions found in a page (the shield flags them; it cannot make a model refuse), content a site really published, and anything that needs access to the operator's own machine or environment variables.

## Deployment

**Local, one user (stdio).** The default. Nothing listens on a port.

**Hosted clients (ChatGPT, Gemini Enterprise, Microsoft 365 Copilot).** Run remote mode and put HTTPS in front of it:

```bash
WIRE_TOKEN=<16+ random characters> uvx agent-wire-cli serve --transport http --remote --port 8765
```

- Remote mode is anonymous only and listens on `127.0.0.1` by default. `--host` can change that, but only together with a token or OAuth; otherwise it refuses to start.
- It does not terminate TLS. Use a reverse proxy, a tunnel or your platform's gateway for HTTPS, and keep the agent-wire port off the public network. If a proxy rewrites the `Host` header, pass `--allowed-host` and `--allowed-origin` for the names clients use.
- Prefer a private path (tunnel, private endpoint, gateway) over a public URL. Never run a public URL without authentication: anyone who finds it fetches pages through your connection.

**Authentication.** Either or both of:

- `--token` or `WIRE_TOKEN`: one shared secret, at least 16 characters, compared in constant time. Simple; it identifies the deployment, not a person.
- OAuth 2.0 access tokens (JWT): `--oauth-issuer`, `--oauth-audience` and `--jwks-uri` (https) or `--jwks-file`. agent-wire checks the signature against the issuer's keys, the issuer, the audience, `sub`, and `exp` (required, 30 seconds of clock leeway); `nbf` is honoured when present. Algorithms are limited to the RS, PS, ES and EdDSA families, so `none` and HMAC tokens are refused, as are tokens over 16 KB or not in compact three-part form. Keys are cached for five minutes and re-fetched, at most once a minute, when a token fails to verify, so key rotation works. It is meant for Microsoft Entra ID, Okta, Auth0, Keycloak and other OIDC providers; it was tested against generated keys, not against each vendor.

**Scopes.** If a token has a `scope` (space separated) or `scp` (list) claim, it limits the tools: `wire:*` allows all, otherwise name the tools (`wire_read wire_search`). A token with no scope claim is not limited by scope; restrict who gets a token at the issuer. A denied call returns `FORBIDDEN`.

## Policy

Put `policy.toml` in `WIRE_HOME`. It applies to every transport, including local stdio.

```toml
allow_domains = ["github.com", "docs.python.org"]   # empty = any public site
deny_domains = ["internal-wiki.example.com"]        # wins over allow_domains
disabled_tools = ["wire_research"]
max_calls_per_minute = 30                           # 0 or absent = unlimited
max_calls_per_day = 2000
max_bytes_per_day = 20000000                        # bytes of tool output returned
max_repeat_calls = 3                                # identical calls within 60 s
kill_switch = "KILL"                                # file name inside WIRE_HOME
audit = true                                        # signed audit log
```

- **Domains** match the host or any subdomain on a dot boundary (`github.com` allows `api.github.com`, not `evilgithub.com` or `github.com.evil.io`). The check runs on every URL the server fetches, including each redirect hop, so an allowed site cannot redirect an agent to a denied one.
- **Budgets** count calls and returned bytes in memory per server process and reset at midnight UTC. A restart resets them; persist them yourself if that matters.
- **Kill switch.** Create the file (an empty file named `KILL` inside `WIRE_HOME`) and every call is refused with `KILLED` until you delete it.
- **Fail closed.** A policy with an unknown key, a wrong type or invalid TOML stops the server from starting. An agent never runs without the limits you wrote down because of a typo.

## Autonomous agents

For agents that run unattended, add `--autonomous` to `serve`:

```bash
uvx agent-wire-cli serve --autonomous
```

It applies ceilings under any policy you wrote (a stricter value in your file wins): 30 calls a minute, 2000 a day, 20 MB of output a day, three identical calls in 60 seconds, and it turns the audit log on. Refusals are machine-readable so an agent can back off instead of retrying:

| Code | Meaning | What an agent should do |
| --- | --- | --- |
| `POLICY_DENIED` | Tool disabled by policy | Stop; do not retry |
| `BUDGET_EXCEEDED` | Minute or daily budget spent | Wait a minute, or until tomorrow (UTC) |
| `LOOP_DETECTED` | Same call repeated | Change the call or stop |
| `KILLED` | An operator pulled the kill switch | Stop; do not retry |
| `FORBIDDEN` | The token's scopes exclude this tool | Ask for a different token |

A blocked URL returns the usual blocked-request error naming the policy reason. `wire_status` shows the budget left and the kill-switch path. A sensible unattended setup: `--autonomous`, an `allow_domains` list, read-only scopes, and the audit log shipped to your SIEM.

## Audit log

With `audit = true` (or `--autonomous`), each tool call (all but the read-only `wire_status`) appends one line to `WIRE_HOME/audit.jsonl`:

```json
{"seq":7,"ts":"2026-11-21T12:00:00.000+00:00","prev":"<sha256 of line 6>","tool":"wire_read","caller":"alice","decision":"allowed","code":"","call":"<hash of arguments>","ms":412,"bytes":3120,"hosts":["github.com"],"sig":"<ed25519>"}
```

- It records the tool, the caller (the OAuth `sub`, `static-token`, or `local`), the decision (`allowed`; `denied` by policy; `error` when the call was blocked or failed and returned an error line), the hosts reached, timing and size. It does not record URL paths, query strings, search text, page content or secrets; arguments appear only as a short hash, enough to correlate repeats.
- Every line is signed with an Ed25519 key kept in `WIRE_HOME` (protected by Windows DPAPI on Windows, a 0600 file elsewhere) and carries the hash of the line before it. Editing, removing, reordering or forging a line is detected:

```bash
uvx agent-wire-cli audit verify
uvx agent-wire-cli audit export > audit-export.jsonl
```

- `verify` exits 1 and says which line broke. Copy `audit.pub` somewhere the server's host cannot write and verify against it with `--public-key`, so someone who owns the server cannot re-sign a rewritten log.
- Truncating the end of the log is only detectable by comparing the last `seq` with an earlier export or your SIEM copy. Ship lines off the host as they are written.
- Run one server process per `WIRE_HOME`; two servers sharing one home would interleave the chain.

## Limits to plan around

- Budgets reset on restart, and the audit log has no external write-once store.
- Remote mode serves public pages only. Intranet or login-only content is out of scope by design.
- The static token is shared; use OAuth when you need per-person audit and scopes.
- agent-wire reports what it blocked and what it stripped. It does not make a model safe to give untrusted input to; keep the agent's own permissions narrow.
