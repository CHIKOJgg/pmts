"""engine/resolution_monitor.py — Detects and handles market resolutions."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ResolutionMonitor:
    def __init__(
        self,
        client: Any,
        markets: List[str],
        on_resolution: Callable[[str, str], None],
        poll_interval_s: float = 300.0,
    ) -> None:
        self._client = client
        self._markets = markets
        self._on_resolution = on_resolution
        self._poll_interval_s = poll_interval_s
        self._task: Optional[asyncio.Task[None]] = None
        self._resolved: Dict[str, str] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop(), name="resolution-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while True:
            for market_id in self._markets:
                if market_id in self._resolved:
                    continue

                try:
                    market = await self._get_market(market_id)
                    if market and market.get("resolved", False):
                        outcome = market.get("winningOutcome", "unknown")
                        self._resolved[market_id] = outcome
                        logger.critical(
                            "Market resolved: %s -> %s", market_id, outcome
                        )
                        self._on_resolution(market_id, outcome)

                        await self._redeem(market_id)
                        logger.info("Redemption triggered for %s", market_id)
                except Exception as e:
                    logger.error("Resolution check failed for %s: %s", market_id, e)

            await asyncio.sleep(self._poll_interval_s)

    async def _get_market(self, market_id: str) -> Optional[Dict[str, Any]]:
        method = getattr(self._client, "get_market", None)
        if method is not None:
            return await method(market_id)  # type: ignore[no-any-return]
        return None

    async def _redeem(self, market_id: str) -> None:
        if hasattr(self._client, "redeem_market"):
            await self._client.redeem_market(market_id)

    @property
    def resolved_markets(self) -> Dict[str, str]:
        return dict(self._resolved)

    def is_resolved(self, market_id: str) -> bool:
        return market_id in self._resolved
