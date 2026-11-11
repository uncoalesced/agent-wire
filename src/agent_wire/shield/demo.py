# Engineered by uncoalesced
"""`wire alerts demo`: run the shield on a bundled page with known injections, offline.

The page hides three attacks (a CSS-hidden instruction, Unicode tag smuggling, an HTML comment)
and openly quotes "ignore previous instructions" in a paragraph about security, which must not
raise an alert. Nothing is fetched: the page ships inside the package.
"""

from importlib.resources import files
from pathlib import Path

from agent_wire.shield import Shield
from agent_wire.shield import events as ev

DEMO_URL = "https://demo.agent-wire.invalid/blog/sqlite-settings"


def demo_html() -> str:
    return files("agent_wire.shield").joinpath("demo.html").read_text(encoding="utf-8")


def run_demo(home: Path, shield: Shield | None = None) -> tuple[str, list]:
    """(what the model would see, raised alerts with ids)."""
    from agent_wire.channels.tier1_global.web import extract_article
    from agent_wire.firewall.inbound_shield import spotlight

    shield = shield or Shield(home)
    with shield.watch("wire alerts demo") as rec:
        token = ev.current_url.set(DEMO_URL)
        try:
            body, _info = extract_article(demo_html())
            text = spotlight(f"[WIRE | demo | {DEMO_URL}]", body)
        finally:
            ev.current_url.reset(token)
    raised = shield.finish(rec) if rec is not None else []
    if raised:
        first, _, rest = text.partition("\n")
        text = f"{first}\n{shield.header(raised)}\n{rest}"
    return text, raised
