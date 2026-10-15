"""Optional stage 4: render in a local headless browser and harvest cf_clearance.

Needs the `stealth` extra (camoufox or playwright). The profile lives in WIRE_HOME, not a temp dir.
"""

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

from agent_wire.engine.http_pool import Fetched

NAV_TIMEOUT_MS = 30_000


class StealthBrowser:
    def __init__(self, home: Path):
        self.profile_dir = home / "browser-profile"

    @staticmethod
    def available() -> bool:
        return bool(importlib.util.find_spec("camoufox") or importlib.util.find_spec("playwright"))

    async def render(self, url: str) -> tuple[Fetched | None, str | None, str | None]:
        if importlib.util.find_spec("camoufox"):
            from camoufox.async_api import AsyncCamoufox

            async with AsyncCamoufox(
                headless=True, persistent_context=True, user_data_dir=str(self.profile_dir)
            ) as context:
                return await _capture(context, url)

        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(self.profile_dir), headless=True
            )
            try:
                return await _capture(context, url)
            finally:
                await context.close()


async def _capture(context, url: str):
    page = await context.new_page()
    response = await page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
    html = await page.content()
    ua = await page.evaluate("navigator.userAgent")
    host = urlsplit(url).hostname or ""
    cf = next(
        (
            c["value"]
            for c in await context.cookies()
            if c["name"] == "cf_clearance" and host.endswith(c["domain"].lstrip("."))
        ),
        None,
    )
    status = response.status if response else 200
    headers = {"content-type": "text/html; charset=utf-8"}
    return Fetched(page.url, status, headers, html.encode(), requested=url), cf, ua
