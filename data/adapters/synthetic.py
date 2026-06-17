"""Synthetic market-data adapters for offline and NL-safe paper soaks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from typing import List, Optional

from data.market_data_provider import _SnapshotCB
from data.models import MarketSnapshot
from src.clock import Clock, LiveClock
from src.enums import Platform

logger = logging.getLogger(__name__)


def _stable_seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass
class _SyntheticMarketState:
    market_id: str
    mid: float
    spread: float
    depth: float
    days_to_resolution: float


class SyntheticMarketFeedAdapter:
    """
    Local, deterministic market-data adapter for offline paper soaks.

    It emits continuous snapshots for a list of markets on one platform and
    never touches live venue infrastructure.
    """

    def __init__(
        self,
        market_ids: List[str],
        platform: Platform,
        taker_fee_bps: int,
        seed: int = 0,
        tick_interval_s: float = 0.5,
        base_mid: float = 0.5,
        spread: float = 0.012,
        volatility: float = 0.008,
        depth_range: tuple[float, float] = (250.0, 2500.0),
        clock: Clock = LiveClock(),
    ) -> None:
        self._market_ids = list(market_ids)
        self._platform = platform
        self._taker_fee_bps = taker_fee_bps
        self._tick_interval_s = tick_interval_s
        self._base_mid = base_mid
        self._base_spread = spread
        self._volatility = volatility
        self._depth_range = depth_range
        self._rng = random.Random(seed)
        self._callback: Optional[_SnapshotCB] = None
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._clock = clock
        self._states: dict[str, _SyntheticMarketState] = {}

        for market_id in self._market_ids:
            self._states[market_id] = _SyntheticMarketState(
                market_id=market_id,
                mid=max(0.05, min(0.95, self._base_mid + self._rng.uniform(-0.12, 0.12))),
                spread=max(0.004, self._base_spread + self._rng.uniform(-0.003, 0.003)),
                depth=self._rng.uniform(*self._depth_range),
                days_to_resolution=self._rng.uniform(0.5, 30.0),
            )

    @property
    def platform(self) -> Platform:
        return self._platform

    def set_snapshot_callback(self, cb: _SnapshotCB) -> None:
        self._callback = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"synthetic-feed-{self._platform.value}")
        logger.info("SyntheticMarketFeedAdapter started for %d markets (%s)", len(self._market_ids), self._platform.value)

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("SyntheticMarketFeedAdapter stopped (%s)", self._platform.value)

    async def _run_loop(self) -> None:
        while self._running:
            for market_id in self._market_ids:
                if not self._running:
                    break
                state = self._states[market_id]
                snapshot = self._next_snapshot(state)
                if self._callback:
                    await self._callback(snapshot)
            await asyncio.sleep(self._tick_interval_s)

    def _next_snapshot(self, state: _SyntheticMarketState) -> MarketSnapshot:
        common = self._rng.gauss(0.0, self._volatility)
        mean_reversion = 0.01 * (0.5 - state.mid)
        state.mid = max(0.02, min(0.98, state.mid + common + mean_reversion))
        state.spread = max(0.004, min(0.03, state.spread + self._rng.uniform(-0.001, 0.001)))
        state.depth = max(self._depth_range[0], min(self._depth_range[1], state.depth * (0.95 + self._rng.random() * 0.1)))
        state.days_to_resolution = max(0.1, state.days_to_resolution - self._tick_interval_s / 86400.0 * 5.0)
        if state.days_to_resolution <= 0.11 and self._rng.random() < 0.02:
            state.days_to_resolution = self._rng.uniform(1.0, 30.0)

        yes_bid = max(0.01, round(state.mid - state.spread / 2, 4))
        yes_ask = min(0.99, round(state.mid + state.spread / 2, 4))
        if yes_bid >= yes_ask:
            yes_ask = min(0.99, yes_bid + 0.01)
        no_bid = max(0.01, round(1.0 - yes_ask, 4))
        no_ask = min(0.99, round(1.0 - yes_bid, 4))
        ts = self._clock.now_ms()

        return MarketSnapshot(
            market_id=state.market_id,
            platform=self._platform,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            bid_depth_usdc=state.depth,
            ask_depth_usdc=state.depth * 0.9,
            taker_fee_bps=self._taker_fee_bps,
            ts=ts,
            received_ts=ts,
            days_to_resolution=state.days_to_resolution,
        )
