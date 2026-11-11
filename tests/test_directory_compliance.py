# Engineered by uncoalesced
"""Claude Directory rules: tool text describes tools, credentials come from userConfig."""

import re

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
