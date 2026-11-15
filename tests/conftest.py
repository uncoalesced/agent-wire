import asyncio
import os

import pytest

from agent_wire.channels.base import Context
from agent_wire.selftest.fakes import FakePool, FakeVault, FakeWaterfall, fetched

os.environ.setdefault("WIRE_SHIELD_NOTIFY", "0")  # tests never pop real desktop notifications

__all__ = ["FakePool", "FakeVault", "FakeWaterfall", "fetched"]  # shared with `wire selftest`


@pytest.fixture
def make_ctx():
    def build(routes, vault=None):
        return Context(pool=None, waterfall=FakeWaterfall(routes), vault=vault)

    return build


@pytest.fixture
def run():
    return asyncio.run
