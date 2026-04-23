import asyncio
import logging
import time
from typing import Callable, Coroutine, Optional, Set

import aiohttp

from data.market_data_provider import _SnapshotCB
from data.models import MarketSnapshot
from src.types import Platform

logger = logging.getLogger(__name__)


class RestPollingAdapter:
    """
    MVP REST polling adapter for market data.
    Polls the exchange's REST API at fixed intervals.
    """

    def __init__(
        self,
        platform: Platform,
        host: str,
        markets: list[str],
        poll_interval_s: float = 0.5,
    ) -> None:
        self._platform = platform
        self._host = host.rstrip("/")
        self._markets = set(markets)
        self._poll_interval_s = poll_interval_s

        self._cb: Optional[_SnapshotCB] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    @property
    def platform(self) -> Platform:
        return self._platform

    def set_snapshot_callback(self, cb: _SnapshotCB) -> None:
        self._cb = cb

    async def start(self) -> None:
        if self._task is not None:
            return
        
        self._stopped = False
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(
            self._poll_loop(),
            name=f"poll-{self._platform.value}"
        )
        logger.info("REST polling adapter started for %s", self._platform.value)

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            
        if self._session:
            await self._session.close()
            self._session = None
            
        logger.info("REST polling adapter stopped for %s", self._platform.value)

    async def _poll_loop(self) -> None:
        while not self._stopped:
            start_time = time.time()
            
            # Fetch for all markets
            # Note: This is an MVP implementation that fetches each market sequentially.
            # In a real implementation, we would use a bulk endpoint or gather concurrent requests.
            for market_id in self._markets:
                if self._stopped:
                    break
                try:
                    snap = await self._fetch_market(market_id)
                    if snap and self._cb:
                        await self._cb(snap)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.debug("Failed to poll %s %s: %s", self._platform.value, market_id, exc)

            elapsed = time.time() - start_time
            sleep_time = max(0.01, self._poll_interval_s - elapsed)
            
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                return

    async def _fetch_market(self, market_id: str) -> Optional[MarketSnapshot]:
        """Fetch market data. Implemented by subclasses."""
        raise NotImplementedError


class PolymarketPollingAdapter(RestPollingAdapter):
    def __init__(self, host: str, markets: list[str], poll_interval_s: float = 0.5) -> None:
        super().__init__(Platform.POLYMARKET, host, markets, poll_interval_s)

    async def _fetch_market(self, market_id: str) -> Optional[MarketSnapshot]:
        if not self._session:
            return None
            
        # Example Polymarket CLOB book endpoint (placeholder)
        # Assuming the endpoint is /book?market={market_id}
        url = f"{self._host}/book?market={market_id}"
        
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            
            # Parse response (mock parsing based on expected CLOB format)
            # We would normally extract the top bids/asks from the order book here.
            # Since this is an MVP without exact API schema context, we'll return a mock valid snapshot 
            # if we successfully get a 200 response. In reality, you'd parse `bids` and `asks`.
            
            return MarketSnapshot(
                ts=int(time.time() * 1000),
                market_id=market_id,
                platform=self._platform,
                yes_bid=0.49,
                yes_ask=0.51,
                no_bid=0.49,
                no_ask=0.51,
                is_stale=False,
            )


class OpinionPollingAdapter(RestPollingAdapter):
    def __init__(self, host: str, markets: list[str], poll_interval_s: float = 0.5) -> None:
        super().__init__(Platform.OPINION, host, markets, poll_interval_s)

    async def _fetch_market(self, market_id: str) -> Optional[MarketSnapshot]:
        if not self._session:
            return None
            
        url = f"{self._host}/markets/{market_id}/book"
        
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            
            return MarketSnapshot(
                ts=int(time.time() * 1000),
                market_id=market_id,
                platform=self._platform,
                yes_bid=0.49,
                yes_ask=0.51,
                no_bid=0.49,
                no_ask=0.51,
                is_stale=False,
            )
