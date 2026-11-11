# Agent Wire by uncoalesced

Live web pages, web search and platform reads (GitHub, Reddit, Hacker News, YouTube, arXiv, Wikipedia and more) for Claude Code and Cowork, from one local MCP server.

- Pages come back compressed to the parts that answer the question.
- Hidden page text (CSS-hidden elements, HTML comments, invisible Unicode) is removed before the model sees it, and everything fetched is marked as untrusted quoted content.
- Saved site logins never leave your machine and are only used for read-only requests.

Install it in Claude Code:

```text
/plugin install agent-wire --marketplace uncoalesced/agent-wire
```

This folder is the Claude plugin. It starts the server with `uvx agent-wire-cli==0.2.5 serve`, the exact release published on PyPI from [github.com/uncoalesced/agent-wire](https://github.com/uncoalesced/agent-wire). Every setting the plugin asks for when you enable it is optional, and each one names the site that receives it.

- Documentation: [DOCUMENTATION.md](https://github.com/uncoalesced/agent-wire/blob/main/DOCUMENTATION.md)
- Privacy Policy: [PRIVACY.md](https://github.com/uncoalesced/agent-wire/blob/main/PRIVACY.md)
- Terms of Use: [TERMS.md](https://github.com/uncoalesced/agent-wire/blob/main/TERMS.md)
- Support: [SUPPORT.md](https://github.com/uncoalesced/agent-wire/blob/main/SUPPORT.md)
- Security reports: [SECURITY.md](https://github.com/uncoalesced/agent-wire/blob/main/SECURITY.md)

## Privacy Policy

agent-wire runs on your machine and sends no telemetry. Its cache, logs and saved logins stay in the plugin's local data folder. Requests go only to the sites you or your agent ask it to read, plus the public services it uses for search and archives. The full policy is in [PRIVACY.md](https://github.com/uncoalesced/agent-wire/blob/main/PRIVACY.md).

MIT License.
