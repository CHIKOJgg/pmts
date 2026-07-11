import asyncio
import json
import logging
from typing import Any, List, Optional

from data.adapters.base_ws import BaseWsAdapter
from data.models import MarketSnapshot
from infrastructure.observability import API_ERRORS_TOTAL, FEED_LAST_TS
from src.clock import Clock, LiveClock
from src.enums import Platform

logger = logging.getLogger(__name__)


class PolymarketWSAdapter(BaseWsAdapter):
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
        market_id_map: Optional[dict[str, str]] = None,
        clock: Clock = LiveClock(),
    ) -> None:
        super().__init__(asset_ids, ws_url, taker_fee_bps, market_id_map, clock)

    async def _subscribe(self, ws: Any) -> None:
        sub_msg = {"type": "subscribe", "assets_ids": self._market_ids, "type_of_market": "clob"}
        await ws.send(json.dumps(sub_msg))
        ack = await asyncio.wait_for(asyncio.ensure_future(ws.recv()), timeout=5.0)
        ack_data = json.loads(ack)
        if ack_data.get("type") == "error":
            raise ConnectionError(f"Polymarket WS subscribe rejected: {ack_data}")
        logger.info("Subscribed to Polymarket assets: %s", self._market_ids)

    async def _handle_message(self, message: Any) -> None:
        try:
            data = json.loads(message)
            # Polymarket WS events: order_book_v2, price, etc.
            if data.get("event_type") != "order_book_v2":
                return

            asset_id = data.get("asset_id")
            market_id = self._market_id_map.get(asset_id, asset_id)
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

            now_ms = self._clock.now_ms()
            ts = int(data.get("timestamp", now_ms))

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
                received_ts=now_ms,
            )

            FEED_LAST_TS.labels(platform=self.PLATFORM.value, market_id=market_id).set(ts / 1000.0)

            if self._callback:
                await self._callback(snapshot)
        except Exception as exc:
            API_ERRORS_TOTAL.labels(platform=self.PLATFORM.value, error_type="parse_error").inc()
            logger.warning("Error parsing Polymarket WS message: %s", exc)
