"""Watchlist: re-run saved topics, report only what is new since the last run, optional webhook."""

from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import RequestException

from agent_wire.firewall.outbound_privacy import scrub
from agent_wire.research.store import norm_topic

MAX_NEW = 10


def check_webhook(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("webhook must be an http(s) URL")
    return url


async def run_watch(wire, watch: dict) -> dict:
    """One watched topic -> fresh stored run + the items the previous run didn't have."""
    topic = watch["topic"]
    result, _, _ = await wire.research_result(topic, days=watch["days"],
                                              platforms=watch.get("sources"), depth="quick",
                                              store=True)  # fmt: skip
    store = wire.store
    prev = store.previous(topic, result.run_id)
    fresh = store.new_items(result.run_id, prev)[:MAX_NEW]
    store.mark_watched(topic, result.run_id)
    report = {"topic": topic, "run_id": result.run_id, "previous_run": prev,
              "new": [{"title": i["title"], "url": i["url"], "source": i["source"], "ts": i["ts"]}
                      for i in fresh]}  # fmt: skip
    if hook := watch.get("webhook"):
        report["webhook"] = await _post(wire, hook, report)
    return report


async def _post(wire, url: str, report: dict) -> str:
    if not report["new"]:
        return "skipped (nothing new)"
    try:
        # The user set this webhook with `wire watch add`; a local receiver is fine.
        f = await wire.pool.get(url, method="POST", impersonate=None, json_body=report,
                                headers={"content-type": "application/json"},
                                allow_private=True)  # fmt: skip
    except RequestException as e:
        return f"failed: {type(e).__name__}"
    return f"HTTP {f.status}"


async def run_watches(wire, topic: str | None = None) -> str:
    watches = wire.store.watches()
    if topic:
        watches = [w for w in watches if w["topic"] == norm_topic(topic)]
    if not watches:
        return "no watched topics (wire watch add <topic>)"
    out = []
    for w in watches:
        r = await run_watch(wire, w)
        head = f"## {r['topic']} · run #{r['run_id']}"
        if r["previous_run"]:
            head += f" · {len(r['new'])} new since run #{r['previous_run']}"
        else:
            head += " · first run"
        if r.get("webhook"):
            head += f" · webhook {r['webhook']}"
        lines = [f"- [{i['title']}]({i['url']}) · {i['source']} · {(i['ts'] or '')[:10]}"
                 for i in r["new"]]  # fmt: skip
        out.append("\n".join([head, *(lines or ["- nothing new"])]))
    return scrub("\n\n".join(out))
