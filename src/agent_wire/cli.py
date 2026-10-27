"""`wire` CLI: serve the MCP server, run the tools from a shell, install into clients, manage logins."""

import asyncio
import re
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
watch_app = typer.Typer(no_args_is_help=True, help="Watched topics: re-run and report what's new.")
app.add_typer(watch_app, name="watch")
library_app = typer.Typer(no_args_is_help=True, help="Saved research briefs: search, list, site.")
app.add_typer(library_app, name="library")

Profile = Annotated[str, typer.Option("--profile", help="standard, or local for small-VRAM models")]


def _utf8() -> None:
    # Windows consoles default to a legacy code page; output contains ★ ♥ · and CJK text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _stdin() -> str:
    # Piped input is UTF-8 whatever the console code page says (cp1252 turns é into Ã©).
    return sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")


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
    code_mode: Annotated[
        bool, typer.Option(help="expose only wire_run (scripts) and wire_status")
    ] = False,
) -> None:
    """Run the MCP server (stdio by default)."""
    from agent_wire.server import create_server

    server = create_server(profile, code_mode=code_mode)
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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="don't ask before writing")] = False,
) -> None:
    """Install into agent clients (auto, all, claude-code, codex, cursor, ...)."""
    from agent_wire.installers.client_installer import detect
    from agent_wire.installers.client_installer import install as do_install

    _utf8()
    if client == "auto" and not dry_run and not yes:
        found = detect(project.resolve(), Path.home())
        typer.echo(f"detected clients: {', '.join(found)}")
        if not typer.confirm("write agent-wire into all of them?", default=True):
            typer.echo("nothing written; pick one with --client <name>")
            raise typer.Exit(1)
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
def research(
    topic: Annotated[
        str, typer.Argument(help="topic, 'A vs B', or empty for what's trending")
    ] = "",
    days: int = 30,
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    mode: Annotated[str, typer.Option(help="auto, topic, compare, discover, person")] = "auto",
    depth: Annotated[str, typer.Option(help="quick, default, deep")] = "default",
    github_user: Annotated[str | None, typer.Option(help="add this GitHub user's activity")] = None,
    as_of: Annotated[str | None, typer.Option(help="end of the window, YYYY-MM-DD")] = None,
    emit: Annotated[str, typer.Option(help="md, json or html")] = "md",
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="write to this file")
    ] = None,
    save_dir: Annotated[
        Path | None, typer.Option(help="write <slug>.<ext> into this folder")
    ] = None,
    store: Annotated[bool, typer.Option(help="save the run to the local library")] = False,
    preflight: Annotated[bool, typer.Option(help="show the plan; no network")] = False,
    verify_links: Annotated[
        bool, typer.Option(help="open web/news links and mark dead ones in the brief")
    ] = True,
    max_tokens: int | None = None,
    profile: Profile = "standard",
) -> None:
    """What people said about a topic in the last N days, across sites, as one cited brief."""
    from agent_wire.server import Wire

    _utf8()

    async def go():
        wire = Wire(profile)
        try:
            return await wire.research(topic, days, platform, mode, depth, as_of, github_user, emit,
                                       store, preflight, max_tokens or 100_000,
                                       verify_links=verify_links)  # fmt: skip
        finally:
            await wire.close()

    text = asyncio.run(go())
    target = output
    if save_dir and not target:
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "trending").lower()).strip("-")[:60] or "brief"
        target = save_dir / f"{slug}-{days}d.{emit}"
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {target}")
    else:
        typer.echo(text)


@app.command()
def cite(
    source: Annotated[
        str, typer.Argument(help="answer text, a file containing it, or - for stdin")
    ] = "-",
    no_quotes: Annotated[bool, typer.Option(help="skip checking quoted text")] = False,
    profile: Profile = "standard",
) -> None:
    """Check an answer's sources: dead links, missing quotes, [[UNSOURCED]] claims."""
    try:
        is_file = len(source) < 260 and "\n" not in source and Path(source).is_file()
    except (OSError, ValueError):
        is_file = False
    if source == "-":
        text = _stdin()
    elif is_file:
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source
    _run(profile, lambda w: w.cite(text, not no_quotes))


@app.command(name="run")
def run_script(
    source: Annotated[
        str, typer.Argument(help="a .js file, or - for stdin (ignored with --eval)")
    ] = "-",
    code: Annotated[str | None, typer.Option("--eval", "-e", help="script text")] = None,
    max_tokens: int | None = None,
    profile: Profile = "standard",
) -> None:
    """Code Mode: run one JS script against wire.* and print only what it returns."""
    if code is None:
        code = _stdin() if source == "-" else Path(source).read_text(encoding="utf-8")
    _run(profile, lambda w: w.run(code, max_tokens))


def _wire_call(profile: str, fn):
    """Run fn(wire) with a Wire that is always closed; print the returned text."""
    from agent_wire.server import Wire

    _utf8()

    async def go():
        wire = Wire(profile)
        try:
            return await fn(wire)
        finally:
            await wire.close()

    typer.echo(asyncio.run(go()))


@watch_app.command("add")
def watch_add(
    topic: str,
    days: int = 7,
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    webhook: Annotated[
        str | None, typer.Option(help="POST new findings (JSON) to this URL")
    ] = None,
) -> None:
    """Watch a topic; `wire watch run` reports what appeared since the last run."""
    from agent_wire.research.planner import normalize_sources
    from agent_wire.research.watch import check_webhook

    try:
        sources = normalize_sources(platform)
        check_webhook(webhook)
    except ValueError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from e

    async def add(w):
        w.store.add_watch(topic, days, ",".join(sources) if sources else None, webhook)
        return (
            f"watching {topic!r} (last {days} days"
            + (f", webhook {webhook}" if webhook else "")
            + ")"
        )

    _wire_call("standard", add)


@watch_app.command("list")
def watch_list() -> None:
    """Watched topics."""

    async def ls(w):
        rows = w.store.watches()
        return (
            "\n".join(
                f"{r['topic']} · {r['days']}d · {r['sources'] or 'default sources'}"
                + (f" · webhook {r['webhook']}" if r["webhook"] else "")
                + (f" · last run #{r['last_run_id']}" if r["last_run_id"] else "")
                for r in rows
            )
            or "no watched topics"
        )

    _wire_call("standard", ls)


@watch_app.command("remove")
def watch_remove(topic: str) -> None:
    """Stop watching a topic."""

    async def rm(w):
        return f"removed {topic!r}" if w.store.remove_watch(topic) else f"not watching {topic!r}"

    _wire_call("standard", rm)


@watch_app.command("run")
def watch_run(
    topic: Annotated[str | None, typer.Argument(help="one topic, or all")] = None,
) -> None:
    """Re-run watched topics and show only new findings (schedule this with cron / Task Scheduler)."""
    from agent_wire.research.watch import run_watches

    _wire_call("standard", lambda w: run_watches(w, topic))


@app.command()
def brief(
    period: Annotated[str, typer.Argument(help="daily or weekly")] = "daily",
    no_trending: bool = False,
) -> None:
    """Digest across every watched topic, plus what's trending; saved under the library."""
    from agent_wire.research.briefing import digest

    _wire_call("standard", lambda w: digest(w, period, trending=not no_trending))


@library_app.command("search")
def library_search(query: str, limit: int = 10) -> None:
    """Offline search over saved briefs."""

    async def s(w):
        return w.library.search_markdown(query, limit)

    _wire_call("standard", s)


@library_app.command("list")
def library_list(limit: int = 20) -> None:
    """Saved runs, newest first."""

    async def ls(w):
        rows = w.store.runs(limit)
        return "\n".join(f"#{r['id']} · {r['created_at'][:16]} · {r['topic']} ({r['mode']}, {r['days']}d)"
                         f" · {r['headline']}" for r in rows) or "library is empty (research --store)"  # fmt: skip

    _wire_call("standard", ls)


@library_app.command("show")
def library_show(run_id: int) -> None:
    """Print a saved brief."""

    async def show(w):
        run = w.store.run(run_id)
        return run["markdown"] if run else f"no run #{run_id}"

    _wire_call("standard", show)


@library_app.command("feed")
def library_feed() -> None:
    """Build WIRE_HOME/library/index.html, feed.xml (Atom) and one page per run."""

    async def build(w):
        out = w.library.build()
        return f"wrote {out / 'index.html'} and {out / 'feed.xml'}"

    _wire_call("standard", build)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@auth_app.command("login")
def auth_login(
    site: str,
    from_browser: Annotated[
        bool, typer.Option("--from-browser", help="copy this site's cookies from a local browser")
    ] = False,
) -> None:
    """Save a session for one site in the encrypted vault (the only cookies agent-wire uses).

    Opens a login window (stealth extra), or with --from-browser copies the site's cookies from
    a browser you're already logged in to.
    """
    from agent_wire.auth.cookie_vault import CookieVault
    from agent_wire.config import init_wire_home

    _utf8()
    vault = CookieVault(init_wire_home())
    if from_browser:
        n = asyncio.run(vault.import_from_browser(site))
        typer.echo(
            f"saved {n} cookies for {site}" if n else f"no {site} cookies found in local browsers"
        )
        return
    try:
        n = asyncio.run(vault.interactive_login(site))
    except ImportError as e:
        typer.echo("wire auth login needs the stealth extra: uv tool install 'agent-wire-cli[stealth]' "
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
