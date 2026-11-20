import json

from test_installer import dirs, entry_of, install  # noqa: F401 - dirs is a fixture

from agent_wire import __version__
from agent_wire.installers import client_installer as ci


def test_failing_second_client_reports_the_first(dirs):  # noqa: F811
    project, _ = dirs
    (project / ".cursor").mkdir()
    (project / ".vscode").mkdir()
    (project / ".vscode" / "mcp.json").write_text("{ // jsonc\n}")
    results = install("auto", dirs)
    by = {r.client: r for r in results}
    assert by["cursor"].files and not by["cursor"].error
    assert "not plain JSON" in by["vscode"].error
    assert (project / ".cursor" / "mcp.json").exists()
    assert (project / ".vscode" / "mcp.json").read_text() == "{ // jsonc\n}"


def test_overwrite_makes_one_backup_and_identical_content_is_untouched(dirs):  # noqa: F811
    _, home = dirs
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps({"numStartups": 3}))
    install("claude-code", dirs, global_=True)
    assert json.loads(cfg.with_name(".claude.json.bak").read_text()) == {"numStartups": 3}
    assert not list(home.rglob("*.tmp"))
    skill = home / ".claude/skills/agent-wire/SKILL.md"
    skill.write_text("my edits")
    install("claude-code", dirs, global_=True)
    assert skill.with_name("SKILL.md.bak").read_text() == "my edits"
    mtime = cfg.stat().st_mtime_ns
    install("claude-code", dirs, global_=True)
    assert cfg.stat().st_mtime_ns == mtime


def test_antigravity_home_is_user_wide_and_gemini_version(dirs):  # noqa: F811
    project, home = dirs
    install("antigravity", dirs)
    user = home / ".gemini" / "antigravity"
    assert entry_of(user / "mcp_config.json")["env"]["WIRE_HOME"] == str(user / ".wire")
    assert (project / ".agent/skills/agent-wire/SKILL.md").exists()
    install("gemini", dirs)
    ext = json.loads((home / ".gemini/extensions/agent-wire/gemini-extension.json").read_text())
    assert ext["version"] == __version__ and ci.__version__ == __version__
