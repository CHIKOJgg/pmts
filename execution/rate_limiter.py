"""execution/rate_limiter.py — Venue-level global rate limiter."""

import asyncio
import time
from collections import deque
from typing import Dict


class VenueRateLimiter:
    """Global singleton rate limiter per venue.

    Unlike asyncio_throttle.Throttler (per-instance), this is shared
    across all clients for the same venue.
    """

    _instances: Dict[str, "VenueRateLimiter"] = {}

    @classmethod
    def for_venue(cls, venue: str, rate_per_s: int) -> "VenueRateLimiter":
        if venue not in cls._instances:
            cls._instances[venue] = cls(rate_per_s)
        return cls._instances[venue]

    def __init__(self, rate_per_s: int) -> None:
        self._rate = rate_per_s
        self._window = 1.0
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()
            if len(self._calls) >= self._rate:
                sleep_for = self._window - (now - self._calls[0])
                await asyncio.sleep(max(0, sleep_for))
            self._calls.append(time.monotonic())