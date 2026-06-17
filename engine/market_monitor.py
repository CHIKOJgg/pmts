"""engine/market_monitor.py - Background market resolution watcher."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketResolution:
    market_id: str
    resolved: bool
    outcome: Optional[str] = None
    resolved_at: Optional[int] = None


@runtime_checkable
class MarketResolutionClient(Protocol):
    async def get_market(self, condition_id: str) -> Any: ...

    async def redeem_market(self, condition_id: str) -> Any: ...


class MarketMonitor:
    """
    Polls active markets and clears resolved ones from the orchestrator.

    The client only needs to expose `get_market(condition_id)` and
    optionally `redeem_market(condition_id)`.
    """

    def __init__(
        self,
        client: MarketResolutionClient,
        orchestrator: Any,
        markets: Optional[Sequence[str]] = None,
        poll_interval_s: float = 300.0,
    ) -> None:
        self._client = client
        self._orchestrator = orchestrator
        self._markets = list(markets) if markets is not None else []
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    def set_markets(self, markets: Sequence[str]) -> None:
        self._markets = list(markets)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="market-monitor")

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

    async def poll_once(self) -> int:
        if not self._markets:
            markets = list(self._orchestrator.get_active_markets())
        else:
            markets = list(self._markets)

        resolved_count = 0
        for market_id in markets:
            market = await self._fetch_market(market_id)
            if market is None or not market.resolved:
                continue

            resolved_count += 1
            logger.critical(
                "Sandbox market resolved market=%s outcome=%s",
                market_id,
                market.outcome or "unknown",
            )
            await self._orchestrator.handle_market_resolution(
                market_id,
                market.outcome,
            )
            redeem = getattr(self._client, "redeem_market", None)
            if callable(redeem):
                try:
                    await redeem(market_id)
                except Exception as exc:
                    logger.error("Redeem failed for %s: %s", market_id, exc)
        return resolved_count

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.poll_once()
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Market monitor loop error: %s", exc, exc_info=True)
                await asyncio.sleep(self._poll_interval_s)

    async def _fetch_market(self, condition_id: str) -> Optional[MarketResolution]:
        market = await self._client.get_market(condition_id)
        if market is None:
            return None

        if isinstance(market, MarketResolution):
            return market

        if isinstance(market, dict):
            return MarketResolution(
                market_id=str(
                    market.get("condition_id")
                    or market.get("market_id")
                    or condition_id
                ),
                resolved=bool(market.get("resolved") or market.get("is_resolved")),
                outcome=(
                    market.get("outcome")
                    or market.get("resolution_outcome")
                    or market.get("winner")
                ),
                resolved_at=market.get("resolved_at"),
            )

        resolved = bool(getattr(market, "resolved", False) or getattr(market, "is_resolved", False))
        outcome = getattr(market, "outcome", None) or getattr(market, "resolution_outcome", None)
        resolved_at = getattr(market, "resolved_at", None)
        return MarketResolution(
            market_id=str(getattr(market, "condition_id", None) or getattr(market, "market_id", condition_id)),
            resolved=resolved,
            outcome=outcome,
            resolved_at=resolved_at,
        )
