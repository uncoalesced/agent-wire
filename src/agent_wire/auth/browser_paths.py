"""Locate browser cookie stores on Windows, macOS and Linux (native, Flatpak, Snap)."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class CookieStore:
    browser: str
    kind: Literal["chromium", "firefox", "safari"]
    path: Path


# browser -> user-data roots, relative to the base dirs below.
_CHROMIUM = {
    "win32": {
        "chrome": ("{local}/Google/Chrome/User Data",),
        "chromium": ("{local}/Chromium/User Data",),
        "brave": ("{local}/BraveSoftware/Brave-Browser/User Data",),
        "edge": ("{local}/Microsoft/Edge/User Data",),
        "vivaldi": ("{local}/Vivaldi/User Data",),
        "opera": ("{roaming}/Opera Software/Opera Stable",),
        "opera_gx": ("{roaming}/Opera Software/Opera GX Stable",),
        "arc": ("{local}/Packages/TheBrowserCompany.Arc_*/LocalCache/Local/Arc/User Data",),
    },
    "darwin": {
        "chrome": ("{appsup}/Google/Chrome",),
        "chromium": ("{appsup}/Chromium",),
        "brave": ("{appsup}/BraveSoftware/Brave-Browser",),
        "edge": ("{appsup}/Microsoft Edge",),
        "vivaldi": ("{appsup}/Vivaldi",),
        "arc": ("{appsup}/Arc/User Data",),
        "opera": ("{appsup}/com.operasoftware.Opera",),
        "opera_gx": ("{appsup}/com.operasoftware.OperaGX",),
    },
    "linux": {
        "chrome": ("{config}/google-chrome", "{home}/.var/app/com.google.Chrome/config/google-chrome"),
        "chromium": (
            "{config}/chromium",
            "{home}/.var/app/org.chromium.Chromium/config/chromium",
            "{home}/snap/chromium/common/chromium",
        ),
        "brave": (
            "{config}/BraveSoftware/Brave-Browser",
            "{home}/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
        ),
        "edge": ("{config}/microsoft-edge", "{home}/.var/app/com.microsoft.Edge/config/microsoft-edge"),
        "vivaldi": ("{config}/vivaldi",),
        "opera": ("{config}/opera",),
    },
}  # fmt: skip

_FIREFOX = {
    "win32": {
        "firefox": ("{roaming}/Mozilla/Firefox/Profiles",),
        "zen": ("{roaming}/zen/Profiles",),
        "librewolf": ("{roaming}/librewolf/Profiles",),
    },
    "darwin": {
        "firefox": ("{appsup}/Firefox/Profiles",),
        "zen": ("{appsup}/zen/Profiles",),
        "librewolf": ("{appsup}/librewolf/Profiles",),
    },
    "linux": {
        "firefox": (
            "{home}/.mozilla/firefox",
            "{home}/.var/app/org.mozilla.firefox/.mozilla/firefox",
            "{home}/snap/firefox/common/.mozilla/firefox",
        ),
        "zen": ("{home}/.zen", "{home}/.var/app/app.zen_browser.zen/.zen"),
        "librewolf": (
            "{home}/.librewolf",
            "{home}/.var/app/io.gitlab.librewolf-community/.librewolf",
        ),
    },
}

_SAFARI = (
    "{home}/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
    "{home}/Library/Cookies/Cookies.binarycookies",
)


def _bases(home: Path, env: dict) -> dict[str, str]:
    return {
        "home": str(home),
        "local": env.get("LOCALAPPDATA") or str(home / "AppData" / "Local"),
        "roaming": env.get("APPDATA") or str(home / "AppData" / "Roaming"),
        "appsup": str(home / "Library" / "Application Support"),
        "config": env.get("XDG_CONFIG_HOME") or str(home / ".config"),
    }


def _expand(pattern: str, bases: dict) -> list[Path]:
    path = pattern.format(**bases)
    if "*" not in path:
        return [Path(path)]
    anchor, _, rest = path.partition("*")
    parent = Path(anchor).parent
    glob = Path(anchor).name + "*" + rest
    return sorted(parent.glob(glob)) if parent.is_dir() else []


def find_cookie_stores(
    home: Path | None = None, platform: str | None = None, env: dict | None = None
) -> list[CookieStore]:
    home = home or Path.home()
    platform = platform or ("linux" if sys.platform.startswith("linux") else sys.platform)
    bases = _bases(home, os.environ if env is None else env)
    stores: list[CookieStore] = []

    for browser, roots in _CHROMIUM.get(platform, {}).items():
        for pattern in roots:
            for root in _expand(pattern, bases):
                profiles = [root] + sorted(
                    p for p in root.glob("*") if p.name == "Default" or p.name.startswith("Profile ")
                ) if root.is_dir() else []  # fmt: skip
                for profile in profiles:
                    for rel in ("Network/Cookies", "Cookies"):
                        if (profile / rel).is_file():
                            stores.append(CookieStore(browser, "chromium", profile / rel))
                            break

    for browser, roots in _FIREFOX.get(platform, {}).items():
        for pattern in roots:
            for root in _expand(pattern, bases):
                if root.is_dir():
                    stores += [
                        CookieStore(browser, "firefox", db)
                        for db in sorted(root.glob("*/cookies.sqlite"))
                    ]

    if platform == "darwin":
        stores += [
            CookieStore("safari", "safari", p)
            for pattern in _SAFARI
            for p in _expand(pattern, bases)
            if p.is_file()
        ]
    return stores
