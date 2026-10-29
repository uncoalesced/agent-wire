"""SSRF guard: agent-supplied URLs never reach loopback, LAN, link-local (cloud metadata),
unique-local, multicast or unspecified addresses, or a non-web scheme.

`check_url` is the cheap static pass (scheme, literal IPs in every form, localhost names) for
entry points; `check_resolved` adds a DNS lookup and runs before every request and redirect hop.
Users who read their own intranet on purpose set WIRE_ALLOW_PRIVATE=1.
"""

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import unquote, urlsplit

from agent_wire.engine.errors import WireBlockedError

ALLOW_ENV = "WIRE_ALLOW_PRIVATE"
SCHEMES = frozenset({"http", "https"})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_PART = re.compile(r"0x[0-9a-f]*|\d+", re.IGNORECASE)
_BAD_NETLOC = frozenset("\\ \t\r\n")  # parser differentials between urlsplit and curl

IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def private_allowed() -> bool:
    return os.environ.get(ALLOW_ENV) == "1"


def is_blocked_ip(ip: IP) -> bool:
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_blocked_ip(embedded)
    return not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """inet_aton forms that curl and OS resolvers accept: 2130706433, 0x7f.1, 0177.0.0.1, 127.1."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(_PART.fullmatch(p) for p in parts):
        return None
    try:
        nums = [int(p[2:] or "0", 16) if p[:2].lower() == "0x"
                else int(p, 8) if len(p) > 1 and p[0] == "0" else int(p) for p in parts]  # fmt: skip
    except ValueError:  # 08, 09: not an address, so the name goes to DNS like any other
        return None
    *head, last = nums
    if any(n > 255 for n in head) or last >= 1 << 8 * (5 - len(nums)):
        return None
    value = last
    for i, n in enumerate(head):
        value |= n << 8 * (3 - i)
    return ipaddress.IPv4Address(value)


def literal_ip(host: str) -> IP | None:
    h = unquote(host).split("%", 1)[0].rstrip(".")  # drop an IPv6 zone id and a trailing dot
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return _legacy_ipv4(h)


def _private(url: str, ip: IP | None = None) -> WireBlockedError:
    where = f" ({ip})" if ip else ""
    return WireBlockedError(
        f"{url}: blocked: private address{where}; set {ALLOW_ENV}=1 to read your own network"
    )


def check_url(url: str) -> None:
    """Raise WireBlockedError for a non-http(s) URL or a host that is plainly private. No DNS."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise WireBlockedError(f"{url}: blocked: malformed URL") from None
    if parts.scheme.lower() not in SCHEMES:
        raise WireBlockedError(f"{url}: blocked: only http(s) URLs are read")
    host = parts.hostname
    if not host or _BAD_NETLOC.intersection(parts.netloc):
        raise WireBlockedError(f"{url}: blocked: malformed URL")
    if private_allowed():
        return
    name = host.rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        raise _private(url)
    if (ip := literal_ip(host)) is not None and is_blocked_ip(ip):
        raise _private(url, ip)


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_resolved(url: str) -> None:
    """`check_url`, then refuse if any address the host resolves to is private.

    An unresolvable host passes: the request itself fails. curl resolves again on connect, so a
    DNS-rebinding server can still race this check; it stops plain names pointing inward.
    """
    check_url(url)
    if private_allowed():
        return
    parts = urlsplit(url)
    if literal_ip(parts.hostname) is not None:
        return
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        raise WireBlockedError(f"{url}: blocked: malformed URL") from None
    try:
        addresses = await _resolve(parts.hostname, port)
    except (OSError, UnicodeError):
        return
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if is_blocked_ip(ip):
            raise _private(url, ip)
