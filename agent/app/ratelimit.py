"""A simple async token-bucket rate limiter for outgoing checks."""

from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Allow at most ``rate`` acquisitions per second (smoothed, with a small burst)."""

    def __init__(self, rate_per_sec: float | None) -> None:
        self.rate = rate_per_sec or 0.0
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)
