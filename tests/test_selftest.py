"""`wire selftest`: the offline run passes, never touches the network, and covers every channel."""

import asyncio
import socket

import pytest
from typer.testing import CliRunner

from agent_wire import selftest as st
from agent_wire.channels import MODULES
from agent_wire.cli import app
from agent_wire.compression import bm25_slicer
from agent_wire.engine.http_pool import HttpPool
from agent_wire.selftest.checks import offline_checks
from agent_wire.selftest.fakes import NetworkUsed, no_network
from agent_wire.selftest.scenarios import scenarios


def test_every_channel_has_a_scenario_and_a_check():
    assert set(scenarios()) == set(MODULES)
    names = {c.name for c in offline_checks() if c.group == "channels"}
    assert set(MODULES) <= names


def test_offline_run_passes_without_network(tmp_path):
    results = st.run(home=tmp_path)
    bad = [(r.group, r.name, r.detail) for r in results if r.status == "fail"]
    assert not bad, bad
    iso = results[-1]
    assert (iso.group, iso.status, iso.detail) == ("isolation", "ok", "0 network attempts")
    assert len(results) == len(offline_checks()) + 1
    assert not any((tmp_path / "selftest").iterdir())  # scratch homes are removed


def test_guard_catches_the_real_http_pool_and_sockets():
    attempts: list[str] = []
    real = socket.getaddrinfo
    with no_network(attempts):
        with pytest.raises(NetworkUsed):
            asyncio.run(HttpPool().get("https://example.com/"))
        with pytest.raises(NetworkUsed):
            socket.getaddrinfo("example.com", 443)
        socket.getaddrinfo("127.0.0.1", 80)  # loopback stays allowed (asyncio needs it)
    assert len(attempts) == 2 and "example.com" in attempts[0]
    assert socket.getaddrinfo is real


def test_only_filters_and_unknown_name_is_refused(tmp_path):
    results = st.run(only="firewall", home=tmp_path)
    assert {r.group for r in results} == {"firewall", "isolation"} and len(results) == 4
    with pytest.raises(ValueError):
        st.run(only="nope", home=tmp_path)


def test_cli_exit_codes(monkeypatch):
    runner = CliRunner()
    ok = [st.Result("x", "y", "ok", "fine")]
    monkeypatch.setattr(st, "run", lambda **kw: ok)
    res = runner.invoke(app, ["selftest"])
    assert res.exit_code == 0 and "1 passed, 0 failed" in res.output
    monkeypatch.setattr(st, "run", lambda **kw: ok + [st.Result("x", "z", "fail", "broke")])
    res = runner.invoke(app, ["selftest", "--json"])
    assert res.exit_code == 1 and '"status": "fail"' in res.output

    def unknown(**kw):
        raise ValueError("no check or group named 'nope'")

    monkeypatch.setattr(st, "run", unknown)
    assert runner.invoke(app, ["selftest", "--only", "nope"]).exit_code == 2


def test_token_counts_fall_back_when_tiktoken_is_unavailable(monkeypatch):
    def offline(name):
        raise ConnectionError("no network")

    bm25_slicer._encoding.cache_clear()
    monkeypatch.setattr(bm25_slicer.tiktoken, "get_encoding", offline)
    try:
        assert not bm25_slicer.exact_tokens()
        text = "Battery life reached eleven hours in our standard loop test. " * 20
        assert 150 < bm25_slicer.count_tokens(text) < 400
        cut = bm25_slicer.truncate_tokens(text, 10)
        assert text.startswith(cut) and bm25_slicer.count_tokens(cut) == 10
    finally:
        bm25_slicer._encoding.cache_clear()
