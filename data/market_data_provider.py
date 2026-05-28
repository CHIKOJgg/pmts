"""data/market_data_provider.py — In-process market data bus."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Coroutine, Dict, List, Optional, Protocol, Set, runtime_checkable

from data.models import MarketSnapshot
from src.clock import Clock, LiveClock
from src.types import Platform

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MS: int = 2_000
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
        stream_writer: Optional[Callable] = None,
        alert_router=None,
        clock: Optional[Clock] = None,
    ) -> None:
        self._index: dict[tuple[str, Platform], MarketSnapshot] = {}
        self._callbacks: list[_SnapshotCB] = []
        self._stream_writer = stream_writer
        self._adapters: List[ExchangeAdapter] = adapters or []
        self._background_tasks: set[asyncio.Task] = set()
        self._alert_router = alert_router
        self._clock = clock or LiveClock()

        for adapter in self._adapters:
            adapter.set_snapshot_callback(self.ingest)

        self.snapshots_received: int = 0
        self.stale_emitted: int = 0
        self.dedup_suppressed: int = 0

        # Circuit breaker for callbacks — disable after consecutive failures
        # Track by callback identity (id) so index shifts don't corrupt state
        self._callback_errors: Dict[int, int] = {}  # id(cb) -> consecutive error count
        self._disabled_callbacks: Set[int] = set()  # id(cb) of disabled callbacks

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
        now = self._clock.now_ms()
        staleness = now - snapshot.ts

        if staleness > STALE_THRESHOLD_MS and not snapshot.is_stale:
            snapshot = snapshot.model_copy(update={"is_stale": True})
            self.stale_emitted += 1

        if staleness > STALE_THRESHOLD_MS * 5 and self._alert_router:
            from infrastructure.alerting import Alert, AlertSeverity

            alert = Alert(
                severity=AlertSeverity.WARNING,
                title="Stale Market Data",
                message=f"Data for {snapshot.market_id} is {staleness}ms old",
                source="MarketDataProvider",
            )
            asyncio.create_task(self._alert_router.send(alert))

        key = (snapshot.market_id, snapshot.platform)
        prev = self._index.get(key)

        if prev is not None and _is_duplicate(prev, snapshot):
            self.dedup_suppressed += 1
            self._index[key] = snapshot  # still update timestamps to prevent false staleness
            return

        self._index[key] = snapshot
        self.snapshots_received += 1

        # Remove any callbacks that were previously disabled
        if self._disabled_callbacks:
            self._callbacks = [cb for cb in self._callbacks if id(cb) not in self._disabled_callbacks]
            # Only clear error counts for the disabled callbacks, keep others intact
            self._callback_errors = {
                k: v for k, v in self._callback_errors.items() if k not in self._disabled_callbacks
            }
            self._disabled_callbacks.clear()

        for cb in self._callbacks:
            cb_id = id(cb)
            try:
                await cb(snapshot)
                self._callback_errors[cb_id] = 0  # Reset on success
            except Exception as exc:
                self._callback_errors[cb_id] = self._callback_errors.get(cb_id, 0) + 1
                logger.error(
                    "Snapshot callback %s raised: %s",
                    cb.__name__ if hasattr(cb, "__name__") else str(cb)[:40],
                    exc,
                    exc_info=True,
                )

                # Circuit breaker: disable callback after too many consecutive errors
                if self._callback_errors[cb_id] > 10:
                    logger.critical(
                        "Callback %s has failed %d times consecutively. Disabling.",
                        cb.__name__ if hasattr(cb, "__name__") else str(cb)[:40],
                        self._callback_errors[cb_id],
                    )
                    self._disabled_callbacks.add(cb_id)

        if self._stream_writer:
            task = asyncio.create_task(
                self._side_write(snapshot),
                name=f"mdb-write-{snapshot.market_id[:8]}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    # ── Reads (hot path) ──────────────────────────────────────────────────────

    def get_snapshot(self, market_id: str, platform: Platform) -> Optional[MarketSnapshot]:
        return self._index.get((market_id, platform))

    def get_mid_prices(self, market_id: str, platform: Platform) -> Optional[tuple[float, float]]:
        snap = self.get_snapshot(market_id, platform)
        if snap is None:
            return None
        return snap.yes_mid, snap.no_mid

    def get_all_markets(self) -> set[str]:
        return {mid for (mid, _) in self._index}

    def get_health(self) -> dict:
        """Check if adapters have received recent data."""
        now = self._clock.now_ms()
        health = {}
        for plat in Platform:
            # Check if we have ANY snapshot for this platform within threshold
            last_ts = 0
            for snap in self._index.values():
                if snap.platform == plat:
                    last_ts = max(last_ts, snap.ts)

            is_alive = (last_ts > 0) and (now - last_ts < STALE_THRESHOLD_MS * 5)
            health[plat.value] = {"alive": is_alive, "last_msg_age_ms": now - last_ts if last_ts > 0 else -1}
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


def _is_significant_change(
    prev: Optional[MarketSnapshot], curr: MarketSnapshot, min_price_change: float = 0.001
) -> bool:
    """Check if there's a meaningful price or depth change."""
    if prev is None:
        return True

    # Check if any price changed by more than minimum threshold
    thresholds = {
        "yes_bid": min_price_change,
        "yes_ask": min_price_change,
        "no_bid": min_price_change,
        "no_ask": min_price_change,
    }

    for attr, thresh in thresholds.items():
        prev_val = getattr(prev, attr)
        curr_val = getattr(curr, attr)
        if abs(prev_val - curr_val) >= thresh:
            return True

    # Also check if depth changed significantly (more than $10)
    if abs(prev.bid_depth_usdc - curr.bid_depth_usdc) > 10.0 or abs(prev.ask_depth_usdc - curr.ask_depth_usdc) > 10.0:
        return True

    # Timestamp changed significantly (more than 500ms)
    if abs(curr.ts - prev.ts) > 500:
        return True

    return False


def _is_duplicate(prev: MarketSnapshot, curr: MarketSnapshot) -> bool:
    """Check if snapshot is effectively duplicate within tolerance."""
    # Use more lenient threshold for deduplication
    return (
        abs(prev.yes_bid - curr.yes_bid) < 1e-6
        and abs(prev.yes_ask - curr.yes_ask) < 1e-6
        and abs(prev.no_bid - curr.no_bid) < 1e-6
        and abs(prev.no_ask - curr.no_ask) < 1e-6
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
