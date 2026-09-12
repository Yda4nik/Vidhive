"""In-process pub/sub for real-time UI updates.

A write endpoint calls ``bus.publish()`` right after its commit; every browser
subscribed to the SSE stream is woken immediately and re-fetches what it shows.
Single coordinator process, so an in-memory fan-out is enough.
"""

from __future__ import annotations

import asyncio


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self) -> None:
        """Signal every subscriber that state changed (non-blocking)."""
        for q in list(self._subs):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass  # a wake-up is already pending for this subscriber


bus = Bus()
