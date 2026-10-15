import sys

import pytest

from agent_wire.config import WIRE_GITIGNORE, get_profile, init_wire_home, resolve_wire_home


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("WIRE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)


def test_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("WIRE_HOME", str(tmp_path / "custom"))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    assert resolve_wire_home() == tmp_path / "custom"


def test_claude_plugin_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    assert resolve_wire_home() == tmp_path / "plugin" / ".wire"


def test_client_dir_detected_from_cwd(monkeypatch, tmp_path):
    nested = tmp_path / ".codex" / "skills" / "agent-wire"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert resolve_wire_home() == tmp_path / ".codex" / ".wire"


def test_os_fallback(monkeypatch, tmp_path):
    if sys.platform == "win32":
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert resolve_wire_home() == tmp_path / "agent-wire"
    else:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert resolve_wire_home() == tmp_path / ".config" / "agent-wire"


def test_init_writes_gitignore_once(tmp_path):
    home = init_wire_home(tmp_path / ".wire")
    gitignore = home / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == WIRE_GITIGNORE
    gitignore.write_text("custom", encoding="utf-8")
    init_wire_home(home)
    assert gitignore.read_text(encoding="utf-8") == "custom"


def test_profiles():
    assert get_profile()["read_max_tokens"] == 3000
    assert get_profile("local")["read_max_tokens"] == 800
    with pytest.raises(ValueError):
        get_profile("turbo")
