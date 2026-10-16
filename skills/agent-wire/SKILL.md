---
name: agent-wire
description: Automatically use this skill whenever the user asks for live web information, shares any URL, asks about social media posts/comments (Reddit, X/Twitter, HackerNews, LinkedIn, YouTube, Bilibili, Xiaohongshu), inspects GitHub repositories/issues, checks market/stock prices, or asks about their own social account activity (e.g., "check my Twitter likes", "my GitHub notifications"). Never ask the user to run a slash command.
---

# Agent-Wire Execution Protocol

You have direct live internet and platform access through the `agent_wire` MCP server (`wire_read`, `wire_search`, `wire_feed`, `wire_me`, `wire_status`).

## Routing Rules (Zero-Turn Overhead)

1. **Never probe health first:** Do NOT call `wire_status` before reading or searching unless the user explicitly asks to diagnose connections. Call `wire_read`, `wire_search`, `wire_feed`, or `wire_me` immediately on Turn 1.

2. **Reading 1-2 URLs:** Call `wire_read(urls=[...], query="<what you need>")` directly. Pass `query` so `agent-wire`'s BM25 engine slices the most relevant sections and comments.

3. **Reading 3+ URLs or Deep Multi-Source Research (Tiered Sub-Agent Fan-Out):**

   - If your environment supports sub-agents (`Task` / `wire-worker`), do NOT dump 5+ raw URLs into your main context. Spawn parallel `wire-worker` sub-agents (fast model tier: Haiku / Luna / Soul) to call `wire_read` and return a 150-word factual synthesis each.

   - If sub-agents are unavailable, pass all URLs in a single batched list: `wire_read(urls=["url1", "url2", "url3"], max_tokens=3000)`.

4. **Searching:** Call `wire_search(query="...", platforms=[...])`. Note that `wire_search` automatically expands the full text of the top 2 results, so you rarely need a follow-up `wire_read` call.

5. **User's Own Account ("my latest post", "my likes", "my notifications"):** Immediately call `wire_me(platform="...", target="...")` which uses the session the user saved with `wire auth login <site>`. If it reports no saved session, tell the user to run that command; never ask for passwords or cookies.
