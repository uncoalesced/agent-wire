import json
import tomllib
from pathlib import Path

import pytest

from agent_wire.installers import client_installer as ci

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    project, home = tmp_path / "proj", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return project, home


def install(client, dirs, **kw):
    project, home = dirs
    return ci.install(client, project=project, home=home, **kw)


def entry_of(path, section="mcpServers"):
    return json.loads(path.read_text(encoding="utf-8"))[section]["agent_wire"]


def test_all_eleven_clients_install(dirs):
    results = install("all", dirs)
    assert [r.client for r in results] == list(ci.CLIENTS) and len(ci.CLIENTS) == 11
    for r in results:
        assert r.files and all(f.exists() for f in r.files), r.client


def test_claude_code_project(dirs):
    project, _ = dirs
    install("claude-code", dirs)
    e = entry_of(project / ".mcp.json")
    assert e["args"][-1] == "serve" and e["command"] in ("uv", "uvx")
    assert e["env"]["WIRE_HOME"] == str(project / ".claude" / ".wire")
    skill = (project / ".claude/skills/agent-wire/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: agent-wire")
    assert "wire-worker" in (project / ".claude/agents/wire-worker.md").read_text(encoding="utf-8")


def test_claude_code_global_merges_existing(dirs):
    _, home = dirs
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({"numStartups": 3, "mcpServers": {"other": {"command": "x"}}}))
    install("claude-code", dirs, global_=True)
    data = json.loads(cfg.read_text())
    assert (
        data["numStartups"] == 3
        and "other" in data["mcpServers"]
        and "agent_wire" in data["mcpServers"]
    )
    assert (home / ".claude/skills/agent-wire/SKILL.md").exists()


def test_idempotent(dirs):
    project, _ = dirs
    install("cursor", dirs)
    first = (project / ".cursor/mcp.json").read_text()
    install("cursor", dirs)
    assert (project / ".cursor/mcp.json").read_text() == first


def test_invalid_json_is_refused_not_clobbered(dirs):
    project, _ = dirs
    bad = project / ".vscode" / "mcp.json"
    bad.parent.mkdir()
    bad.write_text("{ // jsonc comment\n}")
    with pytest.raises(ValueError, match="not plain JSON"):
        install("vscode", dirs)
    assert bad.read_text() == "{ // jsonc comment\n}"


def test_vscode_uses_servers_section(dirs):
    project, _ = dirs
    install("vscode", dirs)
    assert entry_of(project / ".vscode/mcp.json", "servers")["type"] == "stdio"


def test_codex_toml_rerun_keeps_one_block(dirs):
    _, home = dirs
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('model = "gpt-6"\n\n[mcp_servers.other]\ncommand = "x"\n')
    install("codex", dirs)
    install("codex", dirs, profile="local")
    text = cfg.read_text()
    assert text.count("[mcp_servers.agent_wire]") == 1
    data = tomllib.loads(text)
    assert data["model"] == "gpt-6" and "other" in data["mcp_servers"]
    assert data["mcp_servers"]["agent_wire"]["args"][-2:] == ["--profile", "local"]


def test_hermes_yaml_insert_under_existing_key(dirs):
    _, home = dirs
    cfg = home / ".hermes" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("model: x\nmcp_servers:\n  other:\n    command: y\n")
    install("hermes", dirs)
    text = cfg.read_text()
    assert text.count("mcp_servers:") == 1 and "  agent_wire:\n    command: uv" in text
    assert "  other:" in text
    r = install("hermes", dirs)[0]
    assert any("already registers" in n for n in r.notes)


def test_peridot_forces_local_and_gemini_extension(dirs):
    project, home = dirs
    install("peridot", dirs)
    assert entry_of(project / ".peridot/mcp.json")["args"][-2:] == ["--profile", "local"]
    install("gemini", dirs)
    ext = json.loads((home / ".gemini/extensions/agent-wire/gemini-extension.json").read_text())
    assert ext["contextFileName"] == "GEMINI.md" and "agent_wire" in ext["mcpServers"]
    gemini_md = (home / ".gemini/extensions/agent-wire/GEMINI.md").read_text(encoding="utf-8")
    assert not gemini_md.startswith("---") and "wire_read" in gemini_md


def test_auto_detects_and_dry_run_writes_nothing(dirs):
    project, home = dirs
    (project / ".cursor").mkdir()
    (home / ".codex").mkdir()
    results = install("auto", dirs, dry_run=True)
    assert {r.client for r in results} == {"cursor", "codex"}
    assert not (project / ".cursor" / "mcp.json").exists()


def test_auto_defaults_to_claude_code(dirs):
    assert [r.client for r in install("auto", dirs, dry_run=True)] == ["claude-code"]


@pytest.mark.parametrize(
    "direct_url, expected",
    [
        ({"url": "file:///E:/agent-wire", "dir_info": {"editable": True}}, str(Path("E:/agent-wire"))),
        ({"url": "https://github.com/uncoalesced/agent-wire", "vcs_info": {"vcs": "git", "commit_id": "abc"}},
         "git+https://github.com/uncoalesced/agent-wire"),
        ({"url": "https://github.com/u/a", "vcs_info": {"vcs": "git", "requested_revision": "v1"}},
         "git+https://github.com/u/a@v1"),
        ({"url": "https://files.pythonhosted.org/agent_wire.whl", "archive_info": {}}, None),
    ],
)  # fmt: skip
def test_install_source_mirrors_how_it_was_installed(monkeypatch, direct_url, expected):
    class Dist:
        def read_text(self, name):
            return json.dumps(direct_url)

    monkeypatch.setattr(ci.metadata, "distribution", lambda name: Dist())
    assert ci.install_source() == expected
    entry = ci.server_entry("standard", Path("/w"))
    if expected and not expected.startswith("git+"):  # local checkout: reuse its venv, no rebuild
        assert entry["command"] == "uv"
        assert entry["args"] == [
            "run",
            "--quiet",
            "--directory",
            expected,
            "--frozen",
            "wire",
            "serve",
        ]
    else:
        assert entry["command"] == "uvx"
        assert entry["args"] == (["--from", expected] if expected else []) + ["agent-wire", "serve"]


def test_unknown_client(dirs):
    with pytest.raises(ValueError):
        install("notepad", dirs)


def test_manifests_are_valid():
    for name in ("antigravity_mcp.json", "gemini_extension.json", "peridot_local.json"):
        assert "agent_wire" in json.loads((ROOT / "manifests" / name).read_text())["mcpServers"]
    assert "agent_wire" in tomllib.loads((ROOT / "manifests/codex.toml").read_text())["mcp_servers"]
    for name in ("cursor_rule.mdc", "windsurf_rules.md", "hermes_skill.md"):
        assert (ROOT / "manifests" / name).read_text(encoding="utf-8").startswith("---\n")
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert plugin["name"] == "agent-wire"
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert market["plugins"][0]["source"] == "./"
    mcp = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["agent_wire"]
    assert "${CLAUDE_PLUGIN_ROOT:-.}" in mcp["args"]
    worker = (ROOT / "agents/wire-worker.md").read_text(encoding="utf-8")
    assert "mcp__agent_wire__wire_read" in worker and "model: haiku" in worker
    assert ci.asset("SKILL.md") == (ROOT / "skills/agent-wire/SKILL.md").read_text(encoding="utf-8")
