"""Rate limiter timing behaviour."""

import asyncio
import time

from app.ratelimit import AsyncRateLimiter


def test_unlimited_is_fast():
    async def run():
        rl = AsyncRateLimiter(None)
        for _ in range(200):
            await rl.acquire()

    start = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - start < 0.2


def test_limiter_throttles_beyond_burst():
    async def run():
        rl = AsyncRateLimiter(20)  # 20/sec, burst capacity ~20
        for _ in range(30):        # 10 beyond the burst -> ~0.5s of waiting
            await rl.acquire()

    start = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - start
    assert 0.3 < elapsed < 3.0
