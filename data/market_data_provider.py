"""data/market_data_provider.py — In-process market data bus."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Coroutine, List, Optional, Protocol, runtime_checkable

from data.models import MarketSnapshot
from src.types import Platform

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MS: int   = 2_000
SIDE_WRITE_TIMEOUT_S: float = 0.050

_SnapshotCB = Callable[[MarketSnapshot], Coroutine]

@runtime_checkable
class ExchangeAdapter(Protocol):
    """Protocol for exchange data adapters (REST or WebSocket)."""
    @property
    def platform(self) -> Platform: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def set_snapshot_callback(self, cb: _SnapshotCB) -> None: ...


class MarketDataProvider:
    """
    In-process fan-out bus for MarketSnapshot events.
    Manages the lifecycle of exchange adapters.
    Thread-safe reads (dict reads are atomic under GIL).
    All writes come from the asyncio event loop.
    """

    def __init__(
        self, 
        adapters: Optional[List[ExchangeAdapter]] = None,
        stream_writer: Optional[Callable] = None
    ) -> None:
        self._index:        dict[tuple[str, Platform], MarketSnapshot] = {}
        self._callbacks:    list[_SnapshotCB] = []
        self._stream_writer = stream_writer
        self._adapters:     List[ExchangeAdapter] = adapters or []
        self._background_tasks: set[asyncio.Task] = set()

        for adapter in self._adapters:
            adapter.set_snapshot_callback(self.ingest)

        self.snapshots_received:  int = 0
        self.stale_emitted:       int = 0
        self.dedup_suppressed:    int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("MarketDataProvider starting with %d adapters...", len(self._adapters))
        for adapter in self._adapters:
            await adapter.start()
        logger.info("MarketDataProvider started.")

    async def stop(self) -> None:
        logger.info("MarketDataProvider stopping adapters...")
        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception as exc:
                logger.error("Error stopping adapter %s: %s", adapter.platform.value, exc)
        logger.info("MarketDataProvider stopped.")

    # ── Ingestion ─────────────────────────────────────────────────────────────

    async def ingest(self, snapshot: MarketSnapshot) -> None:
        """Accept a snapshot from an exchange adapter."""
        now       = _now_ms()
        staleness = now - snapshot.ts

        if staleness > STALE_THRESHOLD_MS and not snapshot.is_stale:
            snapshot = snapshot.model_copy(update={"is_stale": True})
            self.stale_emitted += 1

        key  = (snapshot.market_id, snapshot.platform)
        prev = self._index.get(key)

        if prev is not None and _is_duplicate(prev, snapshot):
            self.dedup_suppressed += 1
            return

        self._index[key] = snapshot
        self.snapshots_received += 1

        for cb in self._callbacks:
            try:
                await cb(snapshot)
            except Exception as exc:
                logger.error("Snapshot callback raised: %s", exc, exc_info=True)

        if self._stream_writer:
            task = asyncio.create_task(
                self._side_write(snapshot),
                name=f"mdb-write-{snapshot.market_id[:8]}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    # ── Reads (hot path) ──────────────────────────────────────────────────────

    def get_snapshot(
        self, market_id: str, platform: Platform
    ) -> Optional[MarketSnapshot]:
        return self._index.get((market_id, platform))

    def get_mid_prices(
        self, market_id: str, platform: Platform
    ) -> Optional[tuple[float, float]]:
        snap = self.get_snapshot(market_id, platform)
        if snap is None:
            return None
        return snap.yes_mid, snap.no_mid

    def get_all_markets(self) -> set[str]:
        return {mid for (mid, _) in self._index}

    def get_health(self) -> dict:
        """Check if adapters have received recent data."""
        now = _now_ms()
        health = {}
        for plat in Platform:
            # Check if we have ANY snapshot for this platform within threshold
            last_ts = 0
            for snap in self._index.values():
                if snap.platform == plat:
                    last_ts = max(last_ts, snap.ts)
            
            is_alive = (last_ts > 0) and (now - last_ts < STALE_THRESHOLD_MS * 5)
            health[plat.value] = {
                "alive": is_alive,
                "last_msg_age_ms": now - last_ts if last_ts > 0 else -1
            }
        return health

    # ── Subscription ─────────────────────────────────────────────────────────

    def add_callback(self, cb: _SnapshotCB) -> None:
        self._callbacks.append(cb)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _side_write(self, snapshot: MarketSnapshot) -> None:
        try:
            await asyncio.wait_for(
                self._stream_writer("market_snapshots", snapshot.model_dump()),
                timeout=SIDE_WRITE_TIMEOUT_S,
            )
        except Exception as exc:
            logger.debug("Redis side-write failed: %s", exc)


def _is_duplicate(prev: MarketSnapshot, curr: MarketSnapshot) -> bool:
    return (
        prev.yes_bid == curr.yes_bid and
        prev.yes_ask == curr.yes_ask and
        prev.no_bid  == curr.no_bid  and
        prev.no_ask  == curr.no_ask
    )


def _now_ms() -> int:
    return int(time.time() * 1000)