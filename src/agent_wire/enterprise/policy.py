"""WIRE_HOME/policy.toml: what an operator lets agents do through this server.

```toml
allow_domains = ["github.com", "docs.python.org"]   # empty = any public site
deny_domains = ["example-bank.com"]                  # wins over allow_domains
disabled_tools = ["wire_research"]
max_calls_per_minute = 30                            # 0 = unlimited, as for every limit
max_calls_per_day = 2000
max_bytes_per_day = 20000000                         # bytes of tool output returned
max_repeat_calls = 3                                 # identical calls in 60 s before a loop stop
kill_switch = "KILL"                                 # a file with this name in WIRE_HOME halts all calls
audit = true                                         # signed audit log, WIRE_HOME/audit.jsonl
```

A policy that cannot be read or has an unknown key refuses to load: the server does not start
rather than run without the controls the operator wrote down.
"""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from agent_wire.firewall.ssrf_guard import decode_host


class PolicyError(ValueError):
    pass


def _host_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return decode_host(host).lower().rstrip(".")


def _on(host: str, domain: str) -> bool:
    """The domain itself or a subdomain of it: `evilgithub.com` is not github.com."""
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class Policy:
    allow_domains: tuple[str, ...] = ()
    deny_domains: tuple[str, ...] = ()
    disabled_tools: frozenset[str] = field(default_factory=frozenset)
    max_calls_per_minute: int = 0
    max_calls_per_day: int = 0
    max_bytes_per_day: int = 0
    max_repeat_calls: int = 0
    kill_switch: str = "KILL"
    audit: bool = False  # write WIRE_HOME/audit.jsonl (always on under --autonomous)

    def url_refusal(self, url: str) -> str | None:
        """Why this URL may not be fetched, or None."""
        host = _host_of(url)
        if not host:
            return "no host"
        if any(_on(host, d) for d in self.deny_domains):
            return f"{host} is on the deny list"
        if self.allow_domains and not any(_on(host, d) for d in self.allow_domains):
            return f"{host} is not on the allow list"
        return None


# What `--autonomous` adds under any policy the operator wrote: hard ceilings for unattended runs.
AUTONOMOUS = {"max_calls_per_minute": 30, "max_calls_per_day": 2000,
              "max_bytes_per_day": 20_000_000, "max_repeat_calls": 3}  # fmt: skip


def _clean(raw: dict) -> dict:
    known = {f.name for f in fields(Policy)}
    if unknown := sorted(set(raw) - known):
        raise PolicyError(f"unknown policy keys: {', '.join(unknown)}")
    out = {}
    for key, value in raw.items():
        if key in ("allow_domains", "deny_domains", "disabled_tools"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise PolicyError(f"{key} must be a list of strings")
            value = [v.strip().lower().rstrip(".") for v in value if v.strip()]
            out[key] = frozenset(value) if key == "disabled_tools" else tuple(value)
        elif key == "kill_switch":
            if not isinstance(value, str) or not value or "/" in value or "\\" in value:
                raise PolicyError("kill_switch must be a plain file name")
            out[key] = value
        elif key == "audit":
            if not isinstance(value, bool):
                raise PolicyError("audit must be true or false")
            out[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PolicyError(f"{key} must be a whole number, 0 or more")
            out[key] = value
    return out


def parse(text: str, *, autonomous: bool = False) -> Policy:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"policy is not valid TOML: {e}") from e
    values = _clean(raw)
    if autonomous:
        for key, ceiling in AUTONOMOUS.items():
            have = values.get(key, 0)
            values[key] = min(have, ceiling) if have else ceiling
        values["audit"] = True
    return Policy(**values)


def load(home: Path, *, autonomous: bool = False) -> Policy | None:
    """The operator's policy, or None when there is neither a policy file nor --autonomous."""
    path = home / "policy.toml"
    if path.exists():
        try:
            return parse(path.read_text(encoding="utf-8"), autonomous=autonomous)
        except OSError as e:
            raise PolicyError(f"{path} cannot be read: {e}") from e
    return parse("", autonomous=True) if autonomous else None
