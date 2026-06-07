import asyncio
import json
import logging
import time
from typing import Any, List, Optional

import websockets

from data.market_data_provider import _SnapshotCB
from data.models import MarketSnapshot
from infrastructure.observability import API_ERRORS_TOTAL, FEED_LAST_TS, RECONNECT_TOTAL
from src.types import Platform

logger = logging.getLogger(__name__)
ConnectionClosed = getattr(getattr(websockets, "exceptions", None), "ConnectionClosed", Exception)


class OpinionWSAdapter:
    """
    WebSocket adapter for Opinion Markets.
    Subscribes to tickers for a set of markets.
    """

    PLATFORM = Platform.OPINION

    def __init__(
        self,
        market_ids: List[str],
        ws_url: str = "wss://openapi.opinion.trade/openapi/ws",
        taker_fee_bps: int = 25,
        market_id_map: Optional[dict[str, str]] = None,
    ) -> None:
        self._market_ids = market_ids
        self._ws_url = ws_url
        self._taker_fee_bps = taker_fee_bps
        self._market_id_map = market_id_map or {}
        self._callback: Optional[_SnapshotCB] = None

        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    def set_snapshot_callback(self, cb: _SnapshotCB) -> None:
        self._callback = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("OpinionWSAdapter started for %d markets", len(self._market_ids))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OpinionWSAdapter stopped")

    async def _run_loop(self) -> None:
        retry_delay = 1.0
        subscribe_task: Optional[asyncio.Task] = None
        while self._running:
            if subscribe_task and not subscribe_task.done():
                try:
                    subscribe_task.cancel()
                    await subscribe_task
                except asyncio.CancelledError:
                    pass

            RECONNECT_TOTAL.labels(platform=self.PLATFORM.value).inc()
            try:
                async with websockets.connect(self._ws_url) as ws:
                    retry_delay = 1.0

                    # Subscribe to tickers: ticker@marketId
                    params = [f"ticker@{mid}" for mid in self._market_ids]
                    sub_msg = {"method": "SUBSCRIBE", "params": params, "id": int(time.time())}
                    await ws.send(json.dumps(sub_msg))
                    logger.info("Subscribed to Opinion tickers: %s", params)

                    await self._process_messages(ws)
            except Exception as exc:
                if not self._running:
                    break
                logger.error("Opinion WS error: %s. Retrying in %.1fs...", exc, retry_delay)
                API_ERRORS_TOTAL.labels(platform=self.PLATFORM.value, error_type=type(exc).__name__).inc()

                if subscribe_task and not subscribe_task.done():
                    try:
                        subscribe_task.cancel()
                        await subscribe_task
                    except asyncio.CancelledError:
                        pass
                    subscribe_task = None

                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)

    async def _process_messages(self, ws) -> None:
        """Process WebSocket messages in a separate task."""
        try:
            async for message in ws:
                if not self._running:
                    break
                await self._handle_message(message)
        except ConnectionClosed:
            logger.info("Opinion WS connection closed")
        except Exception as exc:
            if self._running:
                logger.error("Error processing Opinion WS messages: %s", exc)

    async def _handle_message(self, message: Any) -> None:
        try:
            data = json.loads(message)
            stream = data.get("stream", "")
            if not stream.startswith("ticker@"):
                return

            venue_market_id = stream.split("@")[1]
            market_id = self._market_id_map.get(venue_market_id, venue_market_id)
            ticker = data.get("data", {})

            yes_bid = float(ticker.get("b", 0.0))
            yes_ask = float(ticker.get("a", 0.0))

            if yes_bid == 0 or yes_ask == 0:
                return

            # Derive NO prices
            no_bid = 1.0 - yes_ask
            no_ask = 1.0 - yes_bid

            # Depth calculation
            bid_depth = float(ticker.get("B", 0.0)) * yes_bid
            ask_depth = float(ticker.get("A", 0.0)) * yes_ask

            ts = int(ticker.get("t", time.time() * 1000))

            snapshot = MarketSnapshot(
                market_id=market_id,
                platform=self.PLATFORM,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                bid_depth_usdc=bid_depth,
                ask_depth_usdc=ask_depth,
                taker_fee_bps=self._taker_fee_bps,
                ts=ts,
                received_ts=int(time.time() * 1000),
            )

            FEED_LAST_TS.labels(platform=self.PLATFORM.value, market_id=market_id).set(ts / 1000.0)

            if self._callback:
                await self._callback(snapshot)
        except Exception as exc:
            API_ERRORS_TOTAL.labels(platform=self.PLATFORM.value, error_type="parse_error").inc()
            logger.warning("Error parsing Opinion WS message: %s", exc)
