"""infrastructure/reconciliation.py — drift detection between local state and the exchange.

The portfolio's mark-to-market is built only from fills we *record locally*.
If an order fills, partially fills, or is cancelled on the exchange while the
process is crashed or a callback is lost, the local view can silently drift
from reality. This module periodically compares the locally-tracked open
orders against the exchange's open-order book and alerts on discrepancies.

It is best-effort and never blocks or alters trading: every external call is
guarded, and the background loop swallows all exceptions so a transient
exchange error can never take down the trading system.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from execution.engine import ExecutionEngine
from infrastructure.alerting import Alert, AlertRouter, AlertSeverity
from src.clock import Clock, LiveClock

logger = logging.getLogger(__name__)


class OrderReconciler:
    """Compares locally-open orders against the exchange's open orders."""

    def __init__(
        self,
        engines: List[ExecutionEngine],
        alert_router: Optional[AlertRouter] = None,
        clock: Optional[Clock] = None,
        interval_s: float = 60.0,
        alert_on_drift: bool = True,
    ) -> None:
        self._engines = engines
        self._alert_router = alert_router
        self._clock = clock or LiveClock()
        self._interval_s = interval_s
        self._alert_on_drift = alert_on_drift
        self._stopped = False
        self._task: Optional[asyncio.Task[None]] = None
        self.last_drift: dict[str, int] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop(), name="order-reconciler")
        logger.info("OrderReconciler started (interval=%.0fs)", self._interval_s)

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._interval_s)
                await self.run_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # never crash the trading system
                logger.warning("OrderReconciler loop error: %s", exc)

    async def run_once(self) -> dict[str, int]:
        """Reconcile every engine once. Returns drift counts per venue."""
        drift: dict[str, int] = {}
        for engine in self._engines:
            try:
                remote_ids = {
                    o.exchange_order_id
                    for o in await engine._client.get_open_orders()
                    if o.exchange_order_id
                }
            except Exception as exc:
                logger.warning(
                    "Reconciliation fetch failed for %s: %s",
                    engine._client.platform.value,
                    exc,
                )
                continue

            local_ids = engine.get_open_exchange_order_ids()
            only_remote = remote_ids - local_ids
            only_local = local_ids - remote_ids
            total = len(only_remote) + len(only_local)
            drift[engine._client.platform.value] = total

            if total > 0:
                msg = (
                    f"Order drift on {engine._client.platform.value}: "
                    f"{len(only_remote)} open on exchange but untracked locally, "
                    f"{len(only_local)} tracked locally but not on exchange"
                )
                logger.warning(msg)
                if self._alert_on_drift and self._alert_router:
                    await self._alert_router.send(
                        Alert(
                            severity=AlertSeverity.WARNING,
                            title="Order Reconciliation Drift",
                            message=msg,
                            source="OrderReconciler",
                            metadata={
                                "platform": engine._client.platform.value,
                                "only_remote": len(only_remote),
                                "only_local": len(only_local),
                            },
                        )
                    )
        self.last_drift = drift
        return drift
