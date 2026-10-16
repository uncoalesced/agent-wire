"""`wire` CLI: serve the MCP server, run the tools from a shell, install into clients, manage logins."""

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer

from agent_wire import __version__

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="agent-wire: agents wired to the live internet.",
)
auth_app = typer.Typer(no_args_is_help=True, help="Local logins for wire_me and login-only sites.")
app.add_typer(auth_app, name="auth")

Profile = Annotated[str, typer.Option("--profile", help="standard, or local for small-VRAM models")]


def _utf8() -> None:
    # Windows consoles default to a legacy code page; output contains ★ ♥ · and CJK text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _run(profile: str, fn) -> None:
    from agent_wire.server import Wire

    _utf8()

    async def go():
        wire = Wire(profile)
        try:
            return await fn(wire)
        finally:
            await wire.close()

    typer.echo(asyncio.run(go()))


@app.command()
def serve(
    profile: Profile = "standard",
    transport: Annotated[str, typer.Option(help="stdio, http or sse")] = "stdio",
    port: int = 8765,
) -> None:
    """Run the MCP server (stdio by default)."""
    from agent_wire.server import create_server

    server = create_server(profile)
    if transport == "stdio":
        server.run(show_banner=False)
    else:
        server.run(transport=transport, port=port, show_banner=False)


@app.command()
def read(
    urls: list[str],
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    section: str | None = None,
    no_comments: bool = False,
    max_tokens: int | None = None,
    force: bool = False,
    profile: Profile = "standard",
) -> None:
    """Read one or more URLs."""
    _run(profile, lambda w: w.read(urls, query, section, not no_comments, max_tokens, force))


@app.command()
def search(
    query: str,
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    limit: int = 5,
    sort: str = "relevance",
    expand: Annotated[int | None, typer.Option(help="top hits to auto-read")] = None,
    max_tokens: int | None = None,
    profile: Profile = "standard",
) -> None:
    """Search the web or platforms (reads the top hits too)."""
    _run(profile, lambda w: w.search(query, platform or ["web"], limit, sort, expand, max_tokens))


@app.command()
def feed(
    platform: str, target: str, sort: str = "hot", limit: int = 10, profile: Profile = "standard"
) -> None:
    """A subreddit, @user timeline, trending repos, market watchlist or RSS feed."""
    _run(profile, lambda w: w.feed(platform, target, sort, limit))


@app.command()
def me(
    platform: str, target: str = "latest_posts", limit: int = 5, profile: Profile = "standard"
) -> None:
    """Your own activity: latest_posts, likes, bookmarks, notifications, stats."""
    _run(profile, lambda w: w.me(platform, target, limit))


@app.command()
def status(refresh: bool = False, profile: Profile = "standard") -> None:
    """Channel health, sessions and route cache."""
    _run(profile, lambda w: w.status(refresh))


@app.command()
def install(
    client: Annotated[str, typer.Option("--client", "-c")] = "auto",
    profile: Profile = "standard",
    global_: Annotated[
        bool, typer.Option("--global", help="user-wide config instead of this project")
    ] = False,
    project: Annotated[Path, typer.Option(help="project root for project-scoped clients")] = Path(
        "."
    ),
    dry_run: bool = False,
) -> None:
    """Install into agent clients (auto, all, claude-code, codex, cursor, ...)."""
    from agent_wire.installers.client_installer import install as do_install

    _utf8()
    try:
        results = do_install(client, project=project.resolve(), home=Path.home(), profile=profile,
                             global_=global_, dry_run=dry_run)  # fmt: skip
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e
    for r in results:
        typer.echo(f"{r.client}:")
        for f in r.files:
            typer.echo(f"  {'would write' if dry_run else 'wrote'} {f}")
        for note in r.notes:
            typer.echo(f"  note: {note}")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@auth_app.command("login")
def auth_login(site: str) -> None:
    """Open a browser window once, log in, close it: cookies go to the encrypted vault."""
    from agent_wire.auth.cookie_vault import CookieVault
    from agent_wire.config import init_wire_home

    _utf8()
    vault = CookieVault(init_wire_home())
    try:
        n = asyncio.run(vault.interactive_login(site))
    except ImportError as e:
        typer.echo("wire auth login needs the stealth extra: uv tool install 'agent-wire[stealth]' "
                   "&& playwright install chromium", err=True)  # fmt: skip
        raise typer.Exit(1) from e
    typer.echo(f"saved {n} cookies for {site}" if n else f"no cookies captured for {site}")


@auth_app.command("list")
def auth_list() -> None:
    """Sites with a saved session (names only, never values)."""
    from agent_wire.auth.cookie_vault import CookieVault
    from agent_wire.config import init_wire_home

    domains = CookieVault(init_wire_home()).domains()
    typer.echo("\n".join(domains) if domains else "no saved sessions")


@auth_app.command("logout")
def auth_logout(site: str) -> None:
    """Forget a saved session."""
    from agent_wire.auth.cookie_vault import CookieVault
    from agent_wire.config import init_wire_home

    removed = CookieVault(init_wire_home()).remove(site)
    typer.echo(f"removed {site}" if removed else f"no saved session for {site}")


if __name__ == "__main__":
    app()
