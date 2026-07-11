import logging
import time
from typing import Any, Dict, List

import aiohttp

from execution.engine import OrderStatusFill
from src.enums import Platform

logger = logging.getLogger(__name__)


class BaseExchangeClient:
    """Shared REST-client plumbing for the Polymarket / Opinion venues.

    Subclasses own venue-specific construction, authentication, order signing,
    and the request/response field mappings. This base provides the session
    lifecycle, tolerant JSON parsing with a venue-specific error key, and the
    fill-delta math that ``get_order_status`` shares across venues.
    """

    PLATFORM: Platform = Platform.POLYMARKET
    _ERROR_KEY: str = "error"

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    def _session_headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._host,
                headers=self._session_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._wallet_private_key = ""
        self._last_status_filled_usdc.clear()

    async def _read_json_or_text(self, resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json()
        except Exception:
            return {self._ERROR_KEY: await resp.text()}

    def _compute_fill_delta(
        self, exchange_order_id: str, cumulative_filled_usdc: float, price: float
    ) -> List[OrderStatusFill]:
        """Return fills detected since the previous status poll for this order."""
        previously_seen = self._last_status_filled_usdc.get(exchange_order_id, 0.0)
        delta_usdc = max(0.0, cumulative_filled_usdc - previously_seen)
        new_fills: List[OrderStatusFill] = []
        if delta_usdc > 0 and price > 0:
            new_fills.append(
                OrderStatusFill(
                    fill_usdc=delta_usdc,
                    fill_price=price,
                    fill_tokens=delta_usdc / price,
                    ts=int(time.time() * 1000),
                )
            )
            self._last_status_filled_usdc[exchange_order_id] = cumulative_filled_usdc
        return new_fills
