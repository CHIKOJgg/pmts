from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine, Optional

import aiohttp

from copytrading.models import WhaleTradeEvent
from copytrading.whale_registry import is_known_whale
from execution.rate_limiter import VenueRateLimiter

logger = logging.getLogger(__name__)

_WhaleTradeCB = Callable[[WhaleTradeEvent], Coroutine[Any, Any, None]]


class WhaleTracker:
    """Polls Polymarket public data for trades by known whale wallets."""

    def __init__(
        self,
        whale_addresses: Optional[list[str]] = None,
        data_url: str = "https://clob.polymarket.com",
        poll_interval_s: float = 10.0,
        max_trades_per_poll: int = 50,
        rate_per_s: int = 5,
    ) -> None:
        self._whale_addresses: set[str] = {
            a.lower() for a in (whale_addresses or [])
        }
        self._data_url = data_url.rstrip("/")
        self._poll_interval_s = poll_interval_s
        self._max_trades_per_poll = max_trades_per_poll
        self._session: Optional[aiohttp.ClientSession] = None
        self._callbacks: list[_WhaleTradeCB] = []
        self._seen_trades: set[str] = set()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._limiter = VenueRateLimiter.for_venue("polymarket_data", rate_per_s)

    def add_callback(self, cb: _WhaleTradeCB) -> None:
        self._callbacks.append(cb)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession(
            base_url=self._data_url,
            headers={"Content-Type": "application/json"},
        )
        self._task = asyncio.create_task(self._poll_loop(), name="whale-tracker")
        logger.info(
            "WhaleTracker started: polling %s every %.1fs, tracking %d addresses",
            self._data_url,
            self._poll_interval_s,
            len(self._whale_addresses),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("WhaleTracker stopped.")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("WhaleTracker poll error: %s", exc)
            await asyncio.sleep(self._poll_interval_s)

    async def _poll_once(self) -> None:
        if not self._session:
            return
        trades = await self._fetch_recent_trades()
        for trade in trades:
            event = self._trade_to_event(trade)
            if event is None:
                continue
            dedup_key = f"{event.wallet_address}:{event.market_id}:{event.ts}:{event.size_usdc}"
            if dedup_key in self._seen_trades:
                continue
            self._seen_trades.add(dedup_key)
            if len(self._seen_trades) > 10_000:
                self._seen_trades.clear()
            logger.info(
                "Whale trade detected: %s %.4f USDC on %s at %.2f",
                event.whale_name or event.wallet_address[:10],
                event.size_usdc,
                event.market_id,
                event.price,
            )
            for cb in self._callbacks:
                try:
                    asyncio.create_task(cb(event))
                except Exception as exc:
                    logger.error("Whale trade callback error: %s", exc)

    async def _fetch_recent_trades(self) -> list[dict[str, Any]]:
        if self._session is None:
            return []
        try:
            await self._limiter.acquire()
            async with self._session.get(
                "/data/trades",
                params={"limit": self._max_trades_per_poll},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.debug("WhaleTracker data/trades returned %d", resp.status)
                    return []
                raw = await resp.json()
                if isinstance(raw, list):
                    return raw
                if isinstance(raw, dict):
                    result = raw.get("trades") or raw.get("data") or []
                    if isinstance(result, list):
                        return result
                return []
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            logger.debug("WhaleTracker fetch error: %s", exc)
            return []

    def _trade_to_event(self, trade: dict[str, Any]) -> Optional[WhaleTradeEvent]:
        maker = (trade.get("maker") or trade.get("owner") or trade.get("user", "")).lower()
        if not maker:
            return None
        is_tracked = maker in self._whale_addresses
        if not is_tracked and not is_known_whale(maker):
            return None
        market_id = (
            trade.get("market")
            or trade.get("market_id")
            or trade.get("condition_id")
            or trade.get("token_id", "")
        )
        side_raw = trade.get("side", "buy")
        side = "buy_yes" if side_raw.upper() in ("BUY", "YES") else "sell_yes"
        size = float(trade.get("size", trade.get("amount", trade.get("makerAmount", 0))))
        price = float(trade.get("price", trade.get("outcomePrice", trade.get("avgPrice", 0))))
        if size <= 0 or price <= 0:
            return None
        ts_raw = trade.get("timestamp", trade.get("ts", int(time.time() * 1000)))
        ts = int(ts_raw) if isinstance(ts_raw, (int, float)) else int(time.time() * 1000)
        return WhaleTradeEvent(
            wallet_address=maker,
            market_id=market_id,
            side=side,
            size_usdc=size,
            price=price,
            ts=ts,
            tx_hash=trade.get("transactionHash", trade.get("tx_hash", "")),
            whale_name=None,
            token_id=trade.get("token_id", trade.get("tokenId")),
        )
