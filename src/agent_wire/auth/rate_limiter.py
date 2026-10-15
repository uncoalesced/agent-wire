"""Per-domain pacing for cookie-bearing requests so agent loops can't get the user's account flagged."""

import asyncio
import random
import time

MIN_INTERVAL_S = 2.0
JITTER_S = (0.15, 0.65)


class RateLimiter:
    def __init__(self, interval: float = MIN_INTERVAL_S, jitter=JITTER_S, clock=None, sleep=None):
        self.interval = interval
        self.jitter = jitter
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            delay = self._next.get(domain, 0.0) - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next[domain] = self._clock() + self.interval + random.uniform(*self.jitter)
