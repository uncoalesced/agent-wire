# Engineered by uncoalesced
"""Out-of-band alert delivery: a desktop notification and a stderr line.

The model must not be the only messenger: a page that hijacked the agent could also make it
leave the warning out. These channels are written by agent-wire itself.

Every notifier runs as argv (never a shell string). Alert text goes to the toast through
environment variables or argv, never spliced into a script, and is sanitized first. Toasts carry
no page text at all, only what agent-wire did and where: `wire alerts show <id>` has the snippet.
"""

import os
import shutil
import subprocess
import sys
import time

from agent_wire.shield.events import T2, TIER_NAMES, Alert, host_of, sanitize

NOTIFY_ENV = "WIRE_SHIELD_NOTIFY"  # "0" turns desktop notifications off
HOST_COOLDOWN_S = 60.0
MAX_PER_MINUTE = 5

_WIN_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
    "ContentType=WindowsRuntime]|Out-Null;"
    "$t=[Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
    "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"
    "$n=$x.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($x.CreateTextNode($env:WIRE_ALERT_TITLE))|Out-Null;"
    "$n.Item(1).AppendChild($x.CreateTextNode($env:WIRE_ALERT_BODY))|Out-Null;"
    "$a='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($a)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)
_MAC_SCRIPT = (
    "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
)


def describe(a: Alert) -> str:
    """One trusted line about an alert. No page text: only what agent-wire did and where."""
    where = host_of(a.url) or a.channel or a.tool or "a page"
    return f"{a.technique} {a.action} on {where}"


def stderr_line(a: Alert, alert_id: int | None) -> str:
    ref = f" (wire alerts show {alert_id})" if alert_id else ""
    return f"[wire-shield] {TIER_NAMES[a.tier].upper()} {sanitize(describe(a), 200)}{ref}"


def _argv(title: str, body: str) -> tuple[list[str], dict[str, str]] | None:
    if sys.platform == "win32":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return None
        env = {**os.environ, "WIRE_ALERT_TITLE": title, "WIRE_ALERT_BODY": body}
        return [ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
                _WIN_SCRIPT], env  # fmt: skip
    if sys.platform == "darwin":
        osa = shutil.which("osascript")
        return ([osa, "-e", _MAC_SCRIPT, title, body], dict(os.environ)) if osa else None
    ns = shutil.which("notify-send")
    if not ns:
        return None
    return [ns, "--app-name=agent-wire", "--", title, body], dict(os.environ)


class Notifier:
    def __init__(self, enabled: bool | None = None, stderr: bool = True):
        self.enabled = os.environ.get(NOTIFY_ENV, "1") != "0" if enabled is None else enabled
        self.stderr = stderr
        self._host_last: dict[str, float] = {}
        self._sent: list[float] = []

    def _allow(self, host: str, now: float) -> bool:
        if now - self._host_last.get(host, -HOST_COOLDOWN_S) < HOST_COOLDOWN_S:
            return False
        self._sent = [t for t in self._sent if now - t < 60]
        if len(self._sent) >= MAX_PER_MINUTE:
            return False
        self._host_last[host] = now
        self._sent.append(now)
        return True

    def notify(self, a: Alert, alert_id: int | None) -> bool:
        """Deliver one alert; True when a desktop notification was launched."""
        if self.stderr:
            print(stderr_line(a, alert_id), file=sys.stderr, flush=True)
        if not self.enabled or not self._allow(host_of(a.url) or a.tool, time.monotonic()):
            return False
        title = "agent-wire: " + ("high-risk request refused" if a.tier >= T2
                                  else "prompt injection on a page")  # fmt: skip
        body = sanitize(describe(a), 180) + (f". Details: wire alerts show {alert_id}"
                                             if alert_id else "")  # fmt: skip
        spec = _argv(title, body)
        if spec is None:
            return False
        argv, env = spec
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # nosec B603 - fixed argv, no shell
                             stderr=subprocess.DEVNULL, creationflags=flags)  # fmt: skip
        except OSError:
            return False
        return True
