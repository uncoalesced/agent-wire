"""Install agent-wire into agent clients, keeping config, cache and vault inside each client's folder.

Existing configs are merged, never clobbered: other servers and keys survive, a file that isn't
valid JSON/TOML is refused instead of overwritten, and re-running is idempotent.
"""

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from urllib.parse import unquote, urlsplit

SERVER = "agent_wire"
CLIENTS = ("claude-code", "codex", "hermes", "antigravity", "claude-desktop", "peridot", "cursor",
           "windsurf", "vscode", "roo", "gemini")  # fmt: skip
ASSETS = {
    "SKILL.md": "skills/agent-wire/SKILL.md",
    "wire-worker.md": "agents/wire-worker.md",
    "cursor_rule.mdc": "manifests/cursor_rule.mdc",
    "windsurf_rules.md": "manifests/windsurf_rules.md",
    "hermes_skill.md": "manifests/hermes_skill.md",
}


@dataclass
class Result:
    client: str
    files: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Target:
    project: Path
    home: Path
    profile: str
    global_: bool
    dry_run: bool


def asset(name: str) -> str:
    """Copy bundled in the wheel, else the source checkout (editable / dev installs)."""
    packaged = resources.files("agent_wire").joinpath("assets", name)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    return (Path(__file__).resolve().parents[3] / ASSETS[name]).read_text(encoding="utf-8")


def _body(md: str) -> str:
    """Markdown without its YAML front matter."""
    return re.sub(r"\A---\n.*?\n---\n+", "", md, flags=re.DOTALL)


def install_source() -> str | None:
    """Where uvx should fetch agent-wire from, mirroring how it was installed (it isn't on PyPI yet).

    A local checkout -> its path; a git install -> `git+<url>[@rev]`; a registry install -> None.
    """
    try:
        raw = metadata.distribution("agent-wire").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    info = json.loads(raw) if raw else {}
    url = info.get("url", "")
    if "vcs_info" in info:
        rev = info["vcs_info"].get("requested_revision")
        return f"git+{url}" + (f"@{rev}" if rev else "")
    if not url.startswith("file:"):
        return None
    path = unquote(urlsplit(url).path)
    if re.match(r"^/[A-Za-z]:", path):  # file:///E:/agent-wire on Windows
        path = path[1:]
    return str(Path(path))


def server_entry(profile: str, wire_home: Path) -> dict:
    """uvx for registry/git installs (cached env); `uv run` for a local checkout, because
    `uvx --from <path>` rebuilds the environment on every start and blows client connect timeouts."""
    src = install_source()
    tail = ["--profile", "local"] if profile == "local" else []
    env = {"WIRE_HOME": str(wire_home)}
    if src and not src.startswith("git+"):
        args = ["run", "--quiet", "--directory", src, "--frozen", "wire", "serve", *tail]
        return {"command": "uv", "args": args, "env": env}
    args = (["--from", src] if src else []) + ["agent-wire", "serve", *tail]
    return {"command": "uvx", "args": args, "env": env}


# ---- file helpers -------------------------------------------------------------------------------
def _write(path: Path, text: str, t: Target, r: Result) -> None:
    if not t.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    r.files.append(path)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _merge_json(path: Path, section: str, entry: dict, t: Target, r: Result) -> None:
    raw = _read(path).strip()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{path} is not plain JSON ({e.msg}); add agent_wire by hand or fix the file"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")  # noqa: TRY004 - CLI reports ValueError
    data.setdefault(section, {})[SERVER] = entry
    _write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", t, r)


def _toml_block(entry: dict) -> str:
    args = ", ".join(json.dumps(a) for a in entry["args"])
    env = ", ".join(f"{k} = {json.dumps(v)}" for k, v in entry["env"].items())
    return f"[mcp_servers.{SERVER}]\ncommand = {json.dumps(entry['command'])}\nargs = [{args}]\nenv = {{ {env} }}\n"


def _merge_toml(path: Path, entry: dict, t: Target, r: Result) -> None:
    text = _read(path)
    block = _toml_block(entry)
    existing = re.compile(rf"(?ms)^\[mcp_servers\.{SERVER}\]\n.*?(?=^\[|\Z)")
    if existing.search(text):
        text = existing.sub(lambda _: block + "\n", text, count=1).rstrip() + "\n"
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block
    try:
        tomllib.loads(text)  # never leave Codex a config it can't parse
    except tomllib.TOMLDecodeError as e:
        raise ValueError(
            f"{path}: merged config would not parse ({e}); add agent_wire by hand"
        ) from e
    _write(path, text, t, r)


def _merge_hermes_yaml(path: Path, entry: dict, t: Target, r: Result) -> None:
    text = _read(path)
    if re.search(rf"(?m)^\s+{SERVER}:\s*$", text):
        r.notes.append(f"{path} already registers {SERVER}; left unchanged")
        return
    block = (f"  {SERVER}:\n    command: {entry['command']}\n    args: {json.dumps(entry['args'])}\n"
             f"    env:\n      WIRE_HOME: {json.dumps(entry['env']['WIRE_HOME'])}\n")  # fmt: skip
    if re.search(r"(?m)^mcp_servers:\s*$", text):
        text = re.sub(r"(?m)^mcp_servers:[ \t]*\n", lambda m: m[0] + block, text, count=1)
    elif re.search(r"(?m)^mcp_servers:", text):
        raise ValueError(f"{path}: mcp_servers uses inline YAML; add agent_wire by hand")
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + "mcp_servers:\n" + block
    _write(path, text, t, r)


def _desktop_dir(home: Path) -> Path:
    if sys.platform == "win32":
        roaming = (
            Path(os.environ["APPDATA"])
            if os.environ.get("APPDATA") and home == Path.home()
            else home / "AppData" / "Roaming"
        )
        return roaming / "Claude"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude"
    return home / ".config" / "Claude"


# ---- clients ------------------------------------------------------------------------------------
def _claude_code(t: Target, r: Result) -> None:
    base = (t.home if t.global_ else t.project) / ".claude"
    entry = server_entry(t.profile, base / ".wire")
    _merge_json(
        t.home / ".claude.json" if t.global_ else t.project / ".mcp.json", "mcpServers", entry, t, r
    )
    _write(base / "skills" / "agent-wire" / "SKILL.md", asset("SKILL.md"), t, r)
    _write(base / "agents" / "wire-worker.md", asset("wire-worker.md"), t, r)


def _codex(t: Target, r: Result) -> None:
    base = (
        t.project / ".codex"
        if not t.global_ and (t.project / ".codex").is_dir()
        else t.home / ".codex"
    )
    _merge_toml(base / "config.toml", server_entry(t.profile, base / ".wire"), t, r)
    _write(base / "skills" / "agent-wire" / "SKILL.md", asset("SKILL.md"), t, r)
    if t.profile != "local":
        r.notes.append(
            "Codex Go plan: re-run with --profile local to cap tool output at 800 tokens"
        )


def _hermes(t: Target, r: Result) -> None:
    base = t.home / ".hermes"
    _merge_hermes_yaml(base / "config.yaml", server_entry(t.profile, base / ".wire"), t, r)
    _write(base / "skills" / "agent-wire" / "SKILL.md", asset("hermes_skill.md"), t, r)


def _antigravity(t: Target, r: Result) -> None:
    base = (t.home / ".gemini" / "antigravity") if t.global_ else (t.project / ".agent")
    entry = server_entry(t.profile, base / ".wire")
    _merge_json(t.home / ".gemini" / "antigravity" / "mcp_config.json", "mcpServers", entry, t, r)
    _write(base / "skills" / "agent-wire" / "SKILL.md", asset("SKILL.md"), t, r)
    _write(base / "agents" / "wire-worker.md", asset("wire-worker.md"), t, r)


def _claude_desktop(t: Target, r: Result) -> None:
    base = _desktop_dir(t.home)
    _merge_json(
        base / "claude_desktop_config.json",
        "mcpServers",
        server_entry(t.profile, base / ".wire"),
        t,
        r,
    )
    r.notes.append("restart Claude Desktop to load the server")


def _peridot(t: Target, r: Result) -> None:
    base = (t.home if t.global_ else t.project) / ".peridot"
    _merge_json(base / "mcp.json", "mcpServers", server_entry("local", base / ".wire"), t, r)
    r.notes.append("peridot always uses the local (8 GB VRAM) profile")


def _cursor(t: Target, r: Result) -> None:
    base = (t.home if t.global_ else t.project) / ".cursor"
    _merge_json(base / "mcp.json", "mcpServers", server_entry(t.profile, base / ".wire"), t, r)
    if not t.global_:
        _write(base / "rules" / "agent-wire.mdc", asset("cursor_rule.mdc"), t, r)


def _windsurf(t: Target, r: Result) -> None:
    base = t.home / ".codeium" / "windsurf"
    _merge_json(
        base / "mcp_config.json", "mcpServers", server_entry(t.profile, base / ".wire"), t, r
    )
    if not t.global_:
        _write(
            t.project / ".windsurf" / "rules" / "agent-wire.md", asset("windsurf_rules.md"), t, r
        )


def _vscode(t: Target, r: Result) -> None:
    base = t.project / ".vscode"
    entry = {"type": "stdio", **server_entry(t.profile, base / ".wire")}
    _merge_json(base / "mcp.json", "servers", entry, t, r)
    if t.global_:
        r.notes.append("VS Code MCP config is per-workspace; wrote .vscode/mcp.json in the project")


def _roo(t: Target, r: Result) -> None:
    base = t.project / ".roo"
    _merge_json(base / "mcp.json", "mcpServers", server_entry(t.profile, base / ".wire"), t, r)
    _write(base / "rules" / "agent-wire.md", _body(asset("SKILL.md")), t, r)


def _gemini(t: Target, r: Result) -> None:
    base = t.home / ".gemini" / "extensions" / "agent-wire"
    manifest = {
        "name": "agent-wire",
        "version": "0.1.0",
        "description": "Live web and platform access: token-compressed, privacy-firewalled.",
        "contextFileName": "GEMINI.md",
        "mcpServers": {SERVER: server_entry(t.profile, base / ".wire")},
    }
    _write(base / "gemini-extension.json", json.dumps(manifest, indent=2) + "\n", t, r)
    _write(base / "GEMINI.md", _body(asset("SKILL.md")), t, r)


WRITERS = {
    "claude-code": _claude_code, "codex": _codex, "hermes": _hermes, "antigravity": _antigravity,
    "claude-desktop": _claude_desktop, "peridot": _peridot, "cursor": _cursor,
    "windsurf": _windsurf, "vscode": _vscode, "roo": _roo, "gemini": _gemini,
}  # fmt: skip


def detect(project: Path, home: Path) -> list[str]:
    markers = {
        "claude-code": [project / ".claude", home / ".claude"],
        "codex": [project / ".codex", home / ".codex"],
        "hermes": [home / ".hermes"],
        "antigravity": [project / ".agent", home / ".gemini" / "antigravity"],
        "claude-desktop": [_desktop_dir(home)],
        "peridot": [project / ".peridot"],
        "cursor": [project / ".cursor", home / ".cursor"],
        "windsurf": [project / ".windsurf", home / ".codeium" / "windsurf"],
        "vscode": [project / ".vscode"],
        "roo": [project / ".roo"],
        "gemini": [home / ".gemini" / "extensions"],
    }
    return [c for c, paths in markers.items() if any(p.exists() for p in paths)] or ["claude-code"]


def install(client: str, *, project: Path, home: Path, profile: str = "standard",
            global_: bool = False, dry_run: bool = False) -> list[Result]:  # fmt: skip
    if client == "all":
        names = list(CLIENTS)
    elif client == "auto":
        names = detect(project, home)
    elif client in WRITERS:
        names = [client]
    else:
        raise ValueError(
            f"unknown client {client!r}; choose auto, all, or one of: {', '.join(CLIENTS)}"
        )
    target = Target(project, home, profile, global_, dry_run)
    results = []
    for name in names:
        r = Result(name)
        WRITERS[name](target, r)
        results.append(r)
    return results
