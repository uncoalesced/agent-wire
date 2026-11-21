"""OAuth 2.0 bearer tokens: validate a JWT access token against an issuer's published keys.

Checks the signature against the issuer's JWKS (algorithm allow-list: no `none`, no HMAC, so a
public key can never be replayed as a secret), then `iss`, `aud`, `exp` (required) and `nbf`.
Scopes come from the `scope` (space separated) or `scp` (list) claim; a scope of `wire:*` or a tool
name such as `wire_read` allows that tool. A token with no scope claim is not restricted by scope.

shortcut: the JWKS is fetched synchronously inside the request that needs it (at most once a minute,
cached five); move it to a background task if an issuer is slow.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA")
CACHE_S, REFRESH_GAP_S, MAX_JWKS_BYTES = 300, 60, 1_000_000


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str] | None = None  # None: not limited by scope

    def allows(self, tool: str) -> bool:
        return self.scopes is None or "wire:*" in self.scopes or tool in self.scopes


def _fetch_jwks(uri: str) -> dict:
    if not uri.lower().startswith("https://"):
        raise ValueError("the JWKS URI must be https")
    from curl_cffi import requests  # the issuer is the operator's choice, so a private one is fine

    r = requests.get(uri, timeout=10, allow_redirects=False)
    if r.status_code != 200 or len(r.content) > MAX_JWKS_BYTES:
        raise ValueError(f"the JWKS endpoint answered {r.status_code}")
    return json.loads(r.content)


class _Keys:
    def __init__(self, jwks_uri: str | None, jwks_file: Path | None, clock):
        if bool(jwks_uri) == bool(jwks_file):
            raise ValueError("give exactly one of a JWKS URI or a JWKS file")
        self.uri, self.file, self.clock = jwks_uri, jwks_file, clock
        self._set: KeySet | None = None
        self._at = self._tried = float("-inf")

    def get(self, force: bool = False) -> KeySet | None:
        now = self.clock()
        stale = self._set is None or now - self._at > CACHE_S
        if (stale or force) and now - self._tried >= (0 if self.file else REFRESH_GAP_S):
            self._tried = now
            try:
                data = (json.loads(self.file.read_text(encoding="utf-8")) if self.file
                        else _fetch_jwks(self.uri))  # fmt: skip
                self._set, self._at = KeySet.import_key_set(data), now
            except (OSError, ValueError, JoseError):
                pass  # keep serving the last good set; with none, every token is refused
        return self._set


def _scopes(claims: dict) -> frozenset[str] | None:
    for name in ("scope", "scp"):
        if name in claims:
            value = claims[name]
            parts = value.split() if isinstance(value, str) else value
            return frozenset(p for p in parts if isinstance(p, str))
    return None


def jwt_authenticator(*, issuer: str, audience: str, jwks_uri: str | None = None,
                      jwks_file: Path | None = None, leeway: int = 30,
                      clock: Callable[[], float] = time.time) -> Callable[[str], Principal | None]:  # fmt: skip
    keys = _Keys(jwks_uri, jwks_file, clock)
    registry = jwt.JWTClaimsRegistry(
        now=lambda: int(clock()), leeway=leeway,
        iss={"essential": True, "value": issuer},
        aud={"essential": True, "value": audience},
        exp={"essential": True}, sub={"essential": True}, nbf={},
    )  # fmt: skip

    def authenticate(presented: str) -> Principal | None:
        if presented.count(".") != 2:  # not a compact JWS: never reach the parser
            return None
        for force in (False, True):  # a second pass picks up a rotated key
            keyset = keys.get(force)
            if keyset is None:
                return None
            try:
                token = jwt.decode(presented, keyset, algorithms=list(ALGORITHMS))
            except (JoseError, ValueError, KeyError):
                continue
            try:
                registry.validate(token.claims)
            except (JoseError, ValueError):
                return None
            return Principal(str(token.claims["sub"]), _scopes(token.claims))
        return None

    return authenticate
