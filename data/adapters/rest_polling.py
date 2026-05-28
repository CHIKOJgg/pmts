import asyncio
import logging
import time
from typing import Optional

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
        taker_fee_bps: int = 20,
    ) -> None:
        self._platform = platform
        self._host = host.rstrip("/")
        self._markets = set(markets)
        self._poll_interval_s = poll_interval_s
        self._taker_fee_bps = taker_fee_bps

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
    def __init__(self, host: str, markets: list[str], poll_interval_s: float = 0.5, taker_fee_bps: int = 20) -> None:
        super().__init__(Platform.POLYMARKET, host, markets, poll_interval_s, taker_fee_bps=taker_fee_bps)

    async def _fetch_market(self, market_id: str) -> Optional[MarketSnapshot]:
        if not self._session:
            return None

        url = f"{self._host}/book?market={market_id}"

        async with self._session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return _snapshot_from_book(data, market_id, self._platform, self._taker_fee_bps)


class OpinionPollingAdapter(RestPollingAdapter):
    def __init__(self, host: str, markets: list[str], poll_interval_s: float = 0.5, taker_fee_bps: int = 25) -> None:
        super().__init__(Platform.OPINION, host, markets, poll_interval_s, taker_fee_bps=taker_fee_bps)

    async def _fetch_market(self, market_id: str) -> Optional[MarketSnapshot]:
        if not self._session:
            return None

        url = f"{self._host}/markets/{market_id}/book"

        async with self._session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return _snapshot_from_book(data, market_id, self._platform, self._taker_fee_bps)


def _snapshot_from_book(
    data: Any,
    market_id: str,
    platform: Platform,
    taker_fee_bps: int,
) -> Optional[MarketSnapshot]:
    if not isinstance(data, dict):
        return None

    yes_bid = _first_float(data, ("yes_bid", "best_yes_bid", "bid", "b"))
    yes_ask = _first_float(data, ("yes_ask", "best_yes_ask", "ask", "a"))
    no_bid = _first_float(data, ("no_bid", "best_no_bid"))
    no_ask = _first_float(data, ("no_ask", "best_no_ask"))

    bids = data.get("bids")
    asks = data.get("asks")
    if yes_bid is None and isinstance(bids, list) and bids:
        yes_bid = _first_float(bids[0], ("price",))
    if yes_ask is None and isinstance(asks, list) and asks:
        yes_ask = _first_float(asks[0], ("price",))

    if yes_bid is None or yes_ask is None:
        return None

    if no_bid is None:
        no_bid = max(0.01, 1.0 - yes_ask)
    if no_ask is None:
        no_ask = min(0.99, 1.0 - yes_bid)

    bid_depth = _depth_from_book(data, "bid_depth_usdc", "bid_depth", bids, yes_bid)
    ask_depth = _depth_from_book(data, "ask_depth_usdc", "ask_depth", asks, yes_ask)

    ts = int(_first_float(data, ("ts", "timestamp", "time")) or time.time() * 1000)

    try:
        return MarketSnapshot(
            market_id=market_id,
            platform=platform,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            bid_depth_usdc=bid_depth,
            ask_depth_usdc=ask_depth,
            taker_fee_bps=taker_fee_bps,
            ts=ts,
            received_ts=int(time.time() * 1000),
            is_stale=False,
        )
    except Exception:
        return None


def _first_float(payload: Any, keys: tuple[str, ...]) -> Optional[float]:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _depth_from_book(
    data: dict,
    direct_key: str,
    alt_key: str,
    levels: Any,
    price: float,
) -> float:
    for key in (direct_key, alt_key):
        value = data.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass

    if isinstance(levels, list) and levels:
        level = levels[0]
        if isinstance(level, dict):
            for key in ("size", "quantity", "qty", "amount", "depth"):
                value = level.get(key)
                if value is not None:
                    try:
                        return float(value) * price
                    except (TypeError, ValueError):
                        continue
    return 0.0
