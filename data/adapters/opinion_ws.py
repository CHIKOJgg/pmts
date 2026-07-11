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


class OpinionWSAdapter(BaseWsAdapter):
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
        clock: Clock = LiveClock(),
    ) -> None:
        super().__init__(market_ids, ws_url, taker_fee_bps, market_id_map, clock)

    async def _subscribe(self, ws: Any) -> None:
        params = [f"ticker@{mid}" for mid in self._market_ids]
        sub_msg = {"method": "SUBSCRIBE", "params": params, "id": self._clock.now_ms()}
        await ws.send(json.dumps(sub_msg))
        ack = await asyncio.wait_for(asyncio.ensure_future(ws.recv()), timeout=5.0)
        ack_data = json.loads(ack)
        if ack_data.get("result") is None:
            raise ConnectionError(f"Opinion WS subscribe rejected: {ack_data}")
        logger.info("Subscribed to Opinion tickers: %s", params)

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

            now_ms = self._clock.now_ms()
            ts = int(ticker.get("t", now_ms))

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
            logger.warning("Error parsing Opinion WS message: %s", exc)
