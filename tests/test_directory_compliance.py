# Engineered by uncoalesced
"""Claude Directory rules: tool text describes tools, credentials come from userConfig."""

import re
from pathlib import Path

from agent_wire import server
from agent_wire.channels.tier1_global import github

DIRECTIVES = re.compile(r"\b(automatically call|call it whenever|do not call|never invent|"
                        r"before sending|prefer it)\b", re.IGNORECASE)  # fmt: skip


def test_server_text_has_no_behavioral_directives():
    texts = [server.INSTRUCTIONS, server.CODE_MODE_INSTRUCTIONS, *server.CODE_MODE.values()]
    for profile in server.DESCRIPTIONS.values():
        texts.extend(profile.values())
    for t in texts:
        assert not DIRECTIVES.search(t), t
    assert "UNTRUSTED_EXTERNAL_DATA" in server.INSTRUCTIONS


def test_shipped_skill_and_agent_text_has_no_overrides():
    """The plugin's skill/agent descriptions say when they apply; they don't override the user."""
    root = Path(__file__).resolve().parents[1] / "plugin"
    overrides = re.compile(r"\b(automatically use|never ask the user|always use this|"
                           r"ignore (the|any) (user|previous))\b", re.IGNORECASE)  # fmt: skip
    files = [*root.glob("skills/*/SKILL.md"), *root.glob("agents/*.md")]
    assert files
    for f in files:
        front = f.read_text(encoding="utf-8").split("---")[1]
        assert not overrides.search(front), (f.name, overrides.search(front)[0])


def test_plugin_lock_pins_this_release():
    """The directory wants a lockfile next to the manifest that declares the uvx server."""
    import tomllib

    from agent_wire import __version__

    plugin = Path(__file__).resolve().parents[1] / "plugin"
    project = tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == [f"agent-wire-cli=={__version__}"]
    lock = plugin / "uv.lock"
    assert lock.stat().st_size < 256 * 1024
    pins = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    assert {"name": "agent-wire-cli", "version": __version__} in [
        {"name": p["name"], "version": p["version"]} for p in pins
    ]


def test_no_gh_cli_guard(monkeypatch):
    calls = []
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("WIRE_NO_GH_CLI", "1")
    monkeypatch.setattr(github.shutil, "which", lambda name: calls.append(name) or "gh")
    github.github_token.cache_clear()
    try:
        assert github.github_token() is None
        assert calls == []
        monkeypatch.setenv("GITHUB_TOKEN", "from-user-config")
        github.github_token.cache_clear()
        assert github.github_token() == "from-user-config"
    finally:
        github.github_token.cache_clear()


def test_status_never_scans_browsers():
    assert "find_cookie_stores" not in vars(server)
