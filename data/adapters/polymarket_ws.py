import asyncio
import json
import logging
import time
from typing import Any, List, Optional

import websockets
from data.market_data_provider import _SnapshotCB
from data.models import MarketSnapshot
from src.types import Platform
from infrastructure.observability import FEED_LAST_TS, RECONNECT_TOTAL, API_ERRORS_TOTAL

logger = logging.getLogger(__name__)

class PolymarketWSAdapter:
    """
    WebSocket adapter for Polymarket CLOB.
    Subscribes to order book updates for a set of assets.
    """
    
    PLATFORM = Platform.POLYMARKET

    def __init__(
        self,
        asset_ids: List[str],
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        taker_fee_bps: int = 20,
    ) -> None:
        self._asset_ids = asset_ids
        self._ws_url = ws_url
        self._taker_fee_bps = taker_fee_bps
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
        logger.info("PolymarketWSAdapter started for %d assets", len(self._asset_ids))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PolymarketWSAdapter stopped")

    async def _run_loop(self) -> None:
        retry_delay = 1.0
        while self._running:
            RECONNECT_TOTAL.labels(platform=self.PLATFORM.value).inc()
            try:
                async with websockets.connect(self._ws_url) as ws:
                    retry_delay = 1.0 # Reset delay on successful connection
                    
                    # Subscribe to all assets
                    # Based on Polymarket CLOB docs: {"type": "subscribe", "assets_ids": [...]}
                    sub_msg = {
                        "type": "subscribe",
                        "assets_ids": self._asset_ids,
                        "type_of_market": "clob"
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info("Subscribed to Polymarket assets: %s", self._asset_ids)

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)
            except Exception as exc:
                if not self._running:
                    break
                logger.error("Polymarket WS error: %s. Retrying in %.1fs...", exc, retry_delay)
                API_ERRORS_TOTAL.labels(platform=self.PLATFORM.value, error_type=type(exc).__name__).inc()
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)

    async def _handle_message(self, message: Any) -> None:
        try:
            data = json.loads(message)
            # Polymarket WS events: order_book_v2, price, etc.
            if data.get("event_type") != "order_book_v2":
                return

            asset_id = data.get("asset_id")
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if not bids or not asks:
                return

            # Best bid and ask
            yes_bid = float(bids[0]["price"])
            yes_ask = float(asks[0]["price"])
            
            # Polymarket property: P(YES) + P(NO) = 1.0
            # So No_Bid = 1.0 - Yes_Ask, No_Ask = 1.0 - Yes_Bid
            no_bid = 1.0 - yes_ask
            no_ask = 1.0 - yes_bid
            
            # Simple depth calculation: top level USDC size
            # bids[0]["size"] is in tokens. USDC depth = tokens * price
            bid_depth = float(bids[0]["size"]) * yes_bid
            ask_depth = float(asks[0]["size"]) * yes_ask

            ts = int(data.get("timestamp", time.time() * 1000))
            
            snapshot = MarketSnapshot(
                market_id=asset_id,
                platform=self.PLATFORM,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                bid_depth_usdc=bid_depth,
                ask_depth_usdc=ask_depth,
                taker_fee_bps=self._taker_fee_bps,
                ts=ts,
                received_ts=int(time.time() * 1000)
            )
            
            FEED_LAST_TS.labels(platform=self.PLATFORM.value, market_id=asset_id).set(ts / 1000.0)

            if self._callback:
                await self._callback(snapshot)
        except Exception as exc:
            API_ERRORS_TOTAL.labels(platform=self.PLATFORM.value, error_type="parse_error").inc()
            logger.debug("Error handling Polymarket WS message: %s", exc)

