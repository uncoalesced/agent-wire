import asyncio

import orjson
import pytest

from agent_wire.channels.base import Context
from agent_wire.engine.http_pool import Fetched


def fetched(body, url="https://example.com/", status=200, ctype=None, requested=None) -> Fetched:
    if isinstance(body, dict | list):
        body, ctype = orjson.dumps(body), ctype or "application/json"
    elif isinstance(body, str):
        body = body.encode()
    return Fetched(url, status, {"content-type": ctype or "text/html; charset=utf-8"}, body,
                   requested=requested or url)  # fmt: skip


class FakeWaterfall:
    """Routes by first matching URL substring; records every call with its kwargs."""

    limiter = None

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def fetch(self, url, **kw):
        self.calls.append((url, kw))
        for needle, resp in self.routes.items():
            if needle in url:
                resp = resp(url, kw) if callable(resp) else resp
                if not isinstance(resp, Fetched):
                    resp = fetched(resp, url=url)
                return resp
        from agent_wire.engine.waterfall import WireFetchError

        raise WireFetchError(f"no fake route for {url}")


class FakeVault:
    def __init__(self, jar=None):
        self.jar = jar or {}

    def get(self, domain):
        return self.jar.get(domain, {})

    async def cookies_for(self, domain):
        return self.jar.get(domain, {})


@pytest.fixture
def make_ctx():
    def build(routes, vault=None):
        return Context(pool=None, waterfall=FakeWaterfall(routes), vault=vault)

    return build


@pytest.fixture
def run():
    return asyncio.run
