"""Fetch errors shared by the engine and the firewall (kept here so neither imports the other)."""


class WireFetchError(Exception):
    pass


class WireBlockedError(WireFetchError):
    """The target is a private / internal address or not an http(s) URL; nothing was sent."""
