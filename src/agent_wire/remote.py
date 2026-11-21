"""Remote mode: serve agent-wire over HTTP to clients that cannot start a local process.

Remote mode is anonymous only. There is no `wire_me`, the cookie vault is never opened, and fetches
of private, loopback or link-local addresses are refused whatever the environment says. A caller
must present a bearer credential unless the server listens on loopback only.
"""

import hmac
import ipaddress
import os
from collections.abc import Callable

from agent_wire.auth.cookie_vault import CookieVault
from agent_wire.enterprise.jwt_auth import Principal
from agent_wire.firewall.ssrf_guard import ALLOW_ENV

TOKEN_ENV = "WIRE_TOKEN"
MIN_TOKEN_LEN = 16
Authenticator = Callable[[str], Principal | None]  # bearer credential -> caller, None to refuse


class RemoteConfigError(ValueError):
    pass


class AnonymousVault(CookieVault):
    """A vault that holds nothing: remote callers never ride on a saved session."""

    def get(self, domain: str) -> dict[str, str]:
        return {}

    def domains(self) -> list[str]:
        return []

    def put(self, domain: str, cookies: dict[str, str]) -> None:
        raise PermissionError("remote mode keeps no sessions")

    def remove(self, domain: str) -> bool:
        return False


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_token(token: str | None) -> str | None:
    token = token or os.environ.get(TOKEN_ENV) or None
    if token is not None and len(token) < MIN_TOKEN_LEN:
        raise RemoteConfigError(f"the token must be at least {MIN_TOKEN_LEN} characters")
    return token


def token_authenticator(token: str) -> Authenticator:
    want = token.encode("utf-8")

    def check(presented: str) -> Principal | None:
        return Principal("static-token") if hmac.compare_digest(presented.encode(), want) else None

    return check


def build_authenticator(token, issuer=None, audience=None, jwks_uri=None, jwks_file=None):
    """Static token, OAuth JWT, both (either one is enough), or None for loopback-only use."""
    static = token_authenticator(secret) if (secret := resolve_token(token)) else None
    if not (issuer or audience or jwks_uri or jwks_file):
        return static
    if not (issuer and audience and bool(jwks_uri) != bool(jwks_file)):
        raise RemoteConfigError(
            "OAuth needs --oauth-issuer, --oauth-audience and one of --jwks-uri or --jwks-file"
        )
    if jwks_uri and not jwks_uri.lower().startswith("https://"):
        raise RemoteConfigError("--jwks-uri must be an https URL")
    from agent_wire.enterprise.jwt_auth import jwt_authenticator

    oauth = jwt_authenticator(issuer=issuer, audience=audience, jwks_uri=jwks_uri,
                              jwks_file=jwks_file)  # fmt: skip
    if static is None:
        return oauth
    return lambda presented: static(presented) or oauth(presented)


def lock_down_environment() -> None:
    """Private-address access can never be switched on for a remote server."""
    os.environ.pop(ALLOW_ENV, None)


class BearerAuth:
    """ASGI middleware: no valid `Authorization: Bearer` header, no request reaches the server."""

    def __init__(self, app, authenticate: Authenticator):
        self.app = app
        self.authenticate = authenticate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        presented = ""
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                scheme, _, rest = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer":
                    presented = rest.strip()
                break
        caller = self.authenticate(presented) if presented else None
        if caller is None:
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"www-authenticate", b'Bearer realm="agent-wire"'),
                (b"content-length", b"12"),
            ]})  # fmt: skip
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return
        scope.setdefault("state", {})["wire_caller"] = caller
        await self.app(scope, receive, send)


def serve_remote(mcp, *, host: str, port: int, authenticate: Authenticator | None,
                 allowed_hosts: list[str] | None = None,
                 allowed_origins: list[str] | None = None) -> None:  # fmt: skip
    from starlette.middleware import Middleware

    if authenticate is None and not is_loopback(host):
        raise RemoteConfigError(
            f"refusing to listen on {host} without authentication: set --token (or {TOKEN_ENV})"
        )
    middleware = [Middleware(BearerAuth, authenticate=authenticate)] if authenticate else None
    mcp.run(transport="http", host=host, port=port, show_banner=False, middleware=middleware,
            allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)  # fmt: skip
