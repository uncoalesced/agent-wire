"""Remote mode: bearer auth, loopback rule, anonymous-only server."""

import asyncio

import pytest
from fastmcp import Client
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from agent_wire import remote as rm
from agent_wire.server import create_server

TOKEN = "t" * 24


@pytest.fixture
def server(tmp_path):
    return create_server(home=tmp_path, remote=True)


def _app(server, authenticate):
    return server.http_app(middleware=[Middleware(rm.BearerAuth, authenticate=authenticate)])


def test_remote_server_has_no_wire_me(server):
    async def names():
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    tools = asyncio.run(names())
    assert "wire_me" not in tools
    assert {"wire_read", "wire_search", "wire_status"} <= tools


def test_remote_vault_is_empty_and_read_only(server):
    assert server.wire.vault.domains() == []
    assert server.wire.vault.get("example.com") == {}
    with pytest.raises(PermissionError):
        server.wire.vault.put("example.com", {"a": "b"})


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong", "Basic " + TOKEN, TOKEN])
def test_requests_without_the_token_get_401(server, header):
    headers = {"authorization": header} if header is not None else {}
    with TestClient(_app(server, rm.token_authenticator(TOKEN))) as c:
        r = c.post("/mcp", headers=headers, json={})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.text == "Unauthorized"


def test_the_token_reaches_the_server(server):
    with TestClient(_app(server, rm.token_authenticator(TOKEN))) as c:
        r = c.post(
            "/mcp",
            headers={
                "authorization": f"Bearer {TOKEN}",
                "accept": "application/json, text/event-stream",
            },
            json={},
        )
    assert r.status_code != 401


def test_token_rules(monkeypatch):
    monkeypatch.delenv(rm.TOKEN_ENV, raising=False)
    assert rm.resolve_token(None) is None
    assert rm.resolve_token(TOKEN) == TOKEN
    with pytest.raises(rm.RemoteConfigError):
        rm.resolve_token("short")
    monkeypatch.setenv(rm.TOKEN_ENV, TOKEN)
    assert rm.resolve_token(None) == TOKEN


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback(host):
    assert rm.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])  # nosec B104
def test_non_loopback_without_auth_is_refused(server, host):
    assert not rm.is_loopback(host)
    with pytest.raises(rm.RemoteConfigError):
        rm.serve_remote(server, host=host, port=1, authenticate=None)


def test_lock_down_clears_private_access(monkeypatch):
    monkeypatch.setenv(rm.ALLOW_ENV, "1")
    rm.lock_down_environment()
    from agent_wire.firewall.ssrf_guard import private_allowed

    assert not private_allowed()
