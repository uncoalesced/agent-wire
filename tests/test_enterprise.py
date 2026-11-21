"""Enterprise controls: policy, budgets, audit chain, OAuth tokens, governed server."""

import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, KeySet, OctKey, RSAKey

from agent_wire import remote as rm
from agent_wire.engine.errors import WireBlockedError
from agent_wire.enterprise import audit, governor, policy
from agent_wire.enterprise.jwt_auth import Principal, jwt_authenticator
from agent_wire.firewall.ssrf_guard import check_url, set_url_policy
from agent_wire.server import Wire

# ---- policy ------------------------------------------------------------------------------------


def test_domain_match_is_on_a_dot_boundary():
    p = policy.parse('allow_domains = ["github.com"]\ndeny_domains = ["gist.github.com"]')
    assert p.url_refusal("https://github.com/a") is None
    assert p.url_refusal("https://api.github.com/a") is None
    assert "not on the allow list" in p.url_refusal("https://evilgithub.com/")
    assert "not on the allow list" in p.url_refusal("https://github.com.evil.io/")
    assert "deny list" in p.url_refusal("https://gist.github.com/x")
    assert p.url_refusal("https://GITHUB.com./x") is None  # case and trailing dot


def test_deny_wins_and_empty_allow_means_any():
    p = policy.parse('deny_domains = ["bad.com"]')
    assert p.url_refusal("https://ok.com/") is None and p.url_refusal("https://x.bad.com/")


@pytest.mark.parametrize(
    "text",
    [
        "bogus = 1",
        "max_calls_per_day = -1",
        "max_calls_per_day = true",
        'allow_domains = "a.com"',
        "allow_domains = [1]",
        'kill_switch = "../x"',
        "audit = 1",
        "not toml [",
    ],
)
def test_bad_policy_refuses_to_load(text):
    with pytest.raises(policy.PolicyError):
        policy.parse(text)


def test_autonomous_caps_a_looser_policy_and_never_raises_a_tighter_one():
    p = policy.parse("max_calls_per_minute = 1000\nmax_calls_per_day = 10", autonomous=True)
    assert p.max_calls_per_minute == 30 and p.max_calls_per_day == 10
    assert p.max_bytes_per_day and p.max_repeat_calls and p.audit


def test_load_none_without_file_or_flag(tmp_path):
    assert policy.load(tmp_path) is None
    assert policy.load(tmp_path, autonomous=True).audit


# ---- governor ----------------------------------------------------------------------------------


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def gov(tmp_path, text="", clock=None, **kw):
    return governor.Governor(tmp_path, policy.parse(text, **kw), clock=clock or Clock())


def admit(g, tool="wire_read", caller=None, args="a"):
    g.admit(tool, caller, g.fingerprint(args))


def test_disabled_tool_and_kill_switch(tmp_path):
    g = gov(tmp_path, 'disabled_tools = ["wire_research"]')
    with pytest.raises(governor.PolicyDenied) as e:
        admit(g, "wire_research")
    assert e.value.code == "POLICY_DENIED" and "do not retry" in e.value.message
    admit(g)
    (tmp_path / "KILL").write_text("")
    with pytest.raises(governor.PolicyDenied) as e:
        admit(g)
    assert e.value.code == "KILLED"


def test_minute_budget_recovers(tmp_path):
    clock = Clock()
    g = gov(tmp_path, "max_calls_per_minute = 2", clock)
    admit(g, args="1")
    admit(g, args="2")
    with pytest.raises(governor.PolicyDenied) as e:
        admit(g, args="3")
    assert e.value.code == "BUDGET_EXCEEDED"
    clock.t += 61
    admit(g, args="3")


def test_daily_call_and_byte_budgets_reset_at_utc_midnight(tmp_path):
    clock = Clock()
    clock.t = 86_400 * 20_000 + 86_000  # 400 s before midnight UTC
    g = gov(tmp_path, "max_calls_per_day = 1\nmax_bytes_per_day = 100", clock)
    admit(g)
    g.done("wire_read", None, "x", "allowed", 1, 100)
    with pytest.raises(governor.PolicyDenied):
        admit(g, args="2")
    clock.t += 500
    admit(g, args="2")
    assert g.remaining()["bytes_today"] == 100


def test_loop_detector_stops_identical_calls_only(tmp_path):
    g = gov(tmp_path, "max_repeat_calls = 2")
    admit(g)
    admit(g)
    admit(g, args="other")
    with pytest.raises(governor.PolicyDenied) as e:
        admit(g)
    assert e.value.code == "LOOP_DETECTED"


def test_scopes_limit_tools(tmp_path):
    g = gov(tmp_path)
    admit(g, caller=Principal("u", frozenset({"wire_read"})))
    with pytest.raises(governor.PolicyDenied) as e:
        admit(g, "wire_search", Principal("u", frozenset({"wire_read"})))
    assert e.value.code == "FORBIDDEN"
    admit(g, "wire_search", Principal("u", frozenset({"wire:*"})))
    admit(g, "wire_search", Principal("u", None))


def test_url_gate_through_the_ssrf_guard_and_host_capture(tmp_path):
    g = gov(tmp_path, 'allow_domains = ["example.com"]')
    set_url_policy(g.url_gate)
    try:
        admit(g)
        check_url("https://www.example.com/a?token=secret")
        with pytest.raises(WireBlockedError, match="not on the allow list"):
            check_url("https://other.org/")
        assert governor._hosts.get() == ["www.example.com"]
    finally:
        set_url_policy(None)


def test_blocked_url_message_drops_the_query_string(tmp_path):
    g = gov(tmp_path, 'deny_domains = ["bad.com"]')
    with pytest.raises(WireBlockedError) as e:
        g.url_gate("https://bad.com/x?token=hunter2")
    assert "hunter2" not in str(e.value)


# ---- audit -------------------------------------------------------------------------------------


def _log(tmp_path, n=3):
    a = audit.AuditLog(tmp_path)
    for i in range(n):
        a.append(tool="wire_read", caller="u", decision="allowed", hosts=[f"h{i}.com"], ms=i)
    return a


def test_audit_chain_verifies_and_survives_a_restart(tmp_path):
    a = _log(tmp_path)
    assert audit.verify(a.path, a.public_key) == (3, None)
    again = audit.AuditLog(tmp_path)  # reloads the key and the chain tail
    again.append(tool="wire_search", caller="u", decision="denied")
    assert audit.verify(a.path, a.public_key) == (4, None)
    assert again.public_key == a.public_key == a.pub_path.read_text().strip()


def test_audit_detects_edit_delete_reorder_and_forgery(tmp_path):
    a = _log(tmp_path, 4)
    lines = a.path.read_text().splitlines()

    def check(new):
        a.path.write_text("\n".join(new) + "\n")
        return audit.verify(a.path, a.public_key)[1]

    assert "signature" in check([lines[0], lines[1].replace('"u"', '"x"'), *lines[2:]])
    assert "sequence" in check([lines[0], *lines[2:]])  # a deleted line
    a._prev = "ab" * 32  # a validly signed line that does not follow its predecessor
    forged = a.append(tool="wire_read", caller="u", decision="allowed")
    a._seq = 2
    forged = json.dumps(
        a.append(tool="wire_read", caller="u", decision="allowed"),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert "chain" in check([lines[0], lines[1], forged])
    assert check([lines[1], lines[0], *lines[2:]])  # reordered
    assert check(lines) is None  # the untouched log still verifies
    other = audit.AuditLog(tmp_path / "other")
    other.append(tool="x", caller="y", decision="allowed")
    assert check(other.path.read_text().splitlines())  # signed by another key
    assert audit.verify(a.path, "zz")[1]


def test_audit_lines_hold_no_urls_or_arguments(tmp_path):
    g = governor.Governor(tmp_path, policy.parse("audit = true"), audit.AuditLog(tmp_path))
    call = g.fingerprint("('https://x.com/p?secret=1',)")
    g.admit("wire_read", None, call)
    g.url_gate("https://x.com/p?secret=1")
    g.done("wire_read", None, call, "allowed", 5, 10)
    text = g.audit.path.read_text()
    assert "secret" not in text and "/p" not in text and '"hosts":["x.com"]' in text


# ---- governed Wire -----------------------------------------------------------------------------


def test_wire_denies_with_a_code_and_audits(tmp_path):
    (tmp_path / "policy.toml").write_text('disabled_tools = ["wire_read"]\naudit = true\n')
    w = Wire(home=tmp_path)

    async def go():
        try:
            return await w.read("https://example.com/")
        finally:
            await w.close()

    out = asyncio.run(go())
    assert out.startswith("[WIRE | POLICY_DENIED | wire_read]")
    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert (rec["decision"], rec["code"], rec["tool"]) == ("denied", "POLICY_DENIED", "wire_read")


def test_wire_without_a_policy_is_ungoverned(tmp_path):
    w = Wire(home=tmp_path)
    assert w.governor is None
    asyncio.run(w.close())


def test_a_broken_policy_stops_the_server(tmp_path):
    (tmp_path / "policy.toml").write_text("nonsense = 1")
    with pytest.raises(policy.PolicyError):
        Wire(home=tmp_path)


# ---- OAuth -------------------------------------------------------------------------------------

ISS, AUD, NOW = "https://idp.example", "agent-wire", 1_800_000_000


@pytest.fixture(scope="module")
def rsa():
    return RSAKey.generate_key(2048, parameters={"kid": "k1"})


def make(rsa_key, tmp_path, **kw):
    f = tmp_path / "jwks.json"
    f.write_text(json.dumps(KeySet([rsa_key]).as_dict(private=False)))
    return jwt_authenticator(issuer=ISS, audience=AUD, jwks_file=f, clock=lambda: NOW, **kw), f


def token(key, alg="RS256", **claims):
    base = {"iss": ISS, "aud": AUD, "sub": "alice", "exp": NOW + 600}
    return jwt.encode({"alg": alg, "kid": "k1"}, {**base, **claims}, key)


def test_a_good_token_yields_a_principal_with_scopes(rsa, tmp_path):
    auth, _ = make(rsa, tmp_path)
    p = auth(token(rsa, scope="wire_read wire_cite"))
    assert p == Principal("alice", frozenset({"wire_read", "wire_cite"}))
    assert auth(token(rsa)).scopes is None
    assert auth(token(rsa, scp=["wire:*"])).allows("wire_feed")


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil"},
        {"aud": "other"},
        {"aud": ["other"]},
        {"exp": NOW - 100},
        {"nbf": NOW + 3600},
    ],
)
def test_bad_claims_are_refused(rsa, tmp_path, claims):
    auth, _ = make(rsa, tmp_path)
    assert auth(token(rsa, **claims)) is None


def test_missing_exp_or_sub_is_refused(rsa, tmp_path):
    auth, _ = make(rsa, tmp_path)
    for drop in ("exp", "sub"):
        claims = {"iss": ISS, "aud": AUD, "sub": "a", "exp": NOW + 60}
        del claims[drop]
        assert auth(jwt.encode({"alg": "RS256", "kid": "k1"}, claims, rsa)) is None


def test_expiry_honours_leeway(rsa, tmp_path):
    auth, _ = make(rsa, tmp_path, leeway=30)
    assert auth(token(rsa, exp=NOW - 10)) is not None
    assert auth(token(rsa, exp=NOW - 31)) is None


def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_alg_none_hmac_confusion_and_wrong_key_are_refused(rsa, tmp_path):
    auth, _ = make(rsa, tmp_path)
    claims = {"iss": ISS, "aud": AUD, "sub": "a", "exp": NOW + 60}
    assert auth(f"{_b64({'alg': 'none'})}.{_b64(claims)}.") is None
    secret = OctKey.import_key("x" * 32)
    assert auth(jwt.encode({"alg": "HS256", "kid": "k1"}, claims, secret)) is None
    # The classic RS->HS downgrade: HMAC-signed with the public key's PEM (joserfc refuses to
    # build this token, so sign it by hand).
    signing = f"{_b64({'alg': 'HS256', 'kid': 'k1'})}.{_b64(claims)}"
    mac = hmac.new(rsa.as_pem(private=False), signing.encode(), hashlib.sha256).digest()
    assert auth(signing + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()) is None
    attacker = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    assert auth(token(attacker)) is None
    for junk in ("", "a.b", "a.b.c", "a.b.c.d", "Bearer x"):
        assert auth(junk) is None


def test_key_rotation_is_picked_up_on_a_failed_verify(rsa, tmp_path):
    auth, f = make(rsa, tmp_path)
    assert auth(token(rsa)) is not None
    new = ECKey.generate_key("P-256", parameters={"kid": "k2"})
    f.write_text(json.dumps(KeySet([rsa, new]).as_dict(private=False)))
    claims = {"iss": ISS, "aud": AUD, "sub": "a", "exp": NOW + 60}
    assert auth(jwt.encode({"alg": "ES256", "kid": "k2"}, claims, new)) is not None


def test_authenticator_wiring_rules(rsa, tmp_path):
    with pytest.raises(rm.RemoteConfigError):
        rm.build_authenticator(None, ISS, None, "https://x/jwks")
    with pytest.raises(rm.RemoteConfigError):
        rm.build_authenticator(None, ISS, AUD, "http://x/jwks")
    with pytest.raises(rm.RemoteConfigError):
        rm.build_authenticator(None, ISS, AUD, "https://x/jwks", tmp_path / "j")
    assert rm.build_authenticator(None) is None
    _, f = make(rsa, tmp_path)
    both = rm.build_authenticator("s" * 20, ISS, AUD, None, f)
    assert both("s" * 20).subject == "static-token" and both("nope") is None
