"""Portable WIRE_HOME resolution and runtime profiles."""

import os
import sys
from pathlib import Path

CLIENT_DIRS = (".claude", ".codex", ".hermes", ".agent", ".cursor", ".windsurf", ".roo", ".gemini")

# Everything in WIRE_HOME (vault.enc, cache.db, venvs) stays out of git except config.
WIRE_GITIGNORE = "*\n!.gitignore\n!config.toml\n"

PROFILES = {
    "standard": {"read_max_tokens": 3000, "search_max_tokens": 2500, "auto_expand_top": 2},
    # IDEA says auto_expand_top=1.5; a result count must be whole, so 1.
    "local": {"read_max_tokens": 800, "search_max_tokens": 800, "auto_expand_top": 1},
}


def _client_dir(path: Path) -> Path | None:
    for p in (path, *path.parents):
        if p.name in CLIENT_DIRS:
            return p
    return None


def resolve_wire_home() -> Path:
    if env := os.environ.get("WIRE_HOME"):
        return Path(env)
    if root := os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return Path(root) / ".wire"
    # Run from inside a client folder, or installed into one (venv lives under it).
    for start in (Path.cwd(), Path(__file__).resolve()):
        if client := _client_dir(start):
            return client / ".wire"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "agent-wire"
    return Path.home() / ".config" / "agent-wire"


def init_wire_home(home: Path | None = None) -> Path:
    home = home or resolve_wire_home()
    home.mkdir(parents=True, exist_ok=True)
    gitignore = home / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(WIRE_GITIGNORE, encoding="utf-8")
    return home


def get_profile(name: str = "standard") -> dict[str, int]:
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; expected one of {sorted(PROFILES)}")
    return PROFILES[name]
