"""In-memory brute-force protection for the login form.

Two sliding windows: failures per (client address, username) stop guessing one
account, and failures per client address stop one source spraying many accounts.
Keyed by the pair, an attacker can only lock *themselves* out of a username — they
cannot lock the real user out from another address. A success clears that pair.

State lives in this process only (the coordinator is a single process); a restart
resets it, which is acceptable for a login throttle.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable


class LoginLimiter:
    def __init__(
        self,
        max_failures: int = 5,
        ip_max_failures: int = 30,
        window_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.ip_max_failures = ip_max_failures
        self.window = window_seconds
        self._clock = clock
        self._pair: dict[tuple[str, str], deque[float]] = {}
        self._ip: dict[str, deque[float]] = {}

    @staticmethod
    def _norm(username: str) -> str:
        return (username or "").strip().lower()[:150]

    def _prune(self, dq: deque[float], now: float) -> None:
        while dq and now - dq[0] >= self.window:
            dq.popleft()

    def blocked_for(self, ip: str, username: str) -> int:
        """Seconds until this attempt would be allowed (0 = allowed now)."""
        now = self._clock()
        wait = 0.0
        for dq, limit in (
            (self._pair.get((ip, self._norm(username))), self.max_failures),
            (self._ip.get(ip), self.ip_max_failures),
        ):
            if dq is None:
                continue
            self._prune(dq, now)
            if len(dq) >= limit:
                wait = max(wait, dq[0] + self.window - now)
        return math.ceil(wait) if wait > 0 else 0

    def record_failure(self, ip: str, username: str) -> None:
        now = self._clock()
        self._pair.setdefault((ip, self._norm(username)), deque()).append(now)
        self._ip.setdefault(ip, deque()).append(now)
        if len(self._pair) > 10_000:
            self._gc(now)

    def record_success(self, ip: str, username: str) -> None:
        self._pair.pop((ip, self._norm(username)), None)

    def _gc(self, now: float) -> None:
        for table in (self._pair, self._ip):
            for key in list(table):
                self._prune(table[key], now)
                if not table[key]:
                    del table[key]
        if len(self._pair) > 50_000:      # still huge: someone is spraying — start over
            self._pair.clear()

    def reset(self) -> None:
        self._pair.clear()
        self._ip.clear()


login_limiter = LoginLimiter()
