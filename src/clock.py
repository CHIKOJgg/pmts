"""src/clock.py — Unified time source for live and simulated modes."""
from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """Protocol for time sources. Implement for live or simulated time."""

    def now_ms(self) -> int:
        """Return current time in milliseconds."""
        ...

    async def sleep_ms(self, ms: int) -> None:
        """Sleep for the given number of milliseconds."""
        ...


class LiveClock:
    """Wall-clock time source for live trading."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep_ms(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000.0)


class SimClock:
    """Simulated time source for backtesting."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        """Advance the simulated clock."""
        self._now += ms

    def advance_to(self, ms: int) -> None:
        """Set the simulated clock to an absolute timestamp."""
        self._now = ms

    async def sleep_ms(self, ms: int) -> None:
        self.advance(ms)
