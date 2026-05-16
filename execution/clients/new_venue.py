"""execution/clients/new_venue.py — Template for new exchange client."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from execution.engine import (
    ExchangeClient,
    OpenOrder,
    OrderStatusResponse,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.types import Platform

logger = logging.getLogger(__name__)


class NewVenueClient(ExchangeClient):
    """Template for new exchange client implementation.

    Extend this class to support new prediction market venues.
    Required methods: place_order, cancel_order, get_order_status, get_open_orders.
    """

    PLATFORM: Platform = Platform.POLYMARKET  # Update for new venue

    def __init__(
        self,
        api_key: str,
        wallet_private_key: str,
        host: str,
        rate_limit_per_s: int = 10,
        sandbox: bool = False,
    ) -> None:
        self._api_key = api_key
        self._wallet_private_key = wallet_private_key
        self._host = host
        self._sandbox = sandbox
        self._rate_limit_per_s = rate_limit_per_s

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        """Place an order on the venue.

        Args:
            submission: Order details (market, side, size, price).
            effective_price: Final execution price after venue-specific adjustments.
            nonce: Optional nonce for replay protection.

        Returns:
            PlacedOrderResponse with exchange_order_id and status.
        """
        raise NotImplementedError

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """Cancel an existing order.

        Args:
            exchange_order_id: ID returned by place_order.
            market_id: Market identifier.

        Returns:
            True if cancellation was successful.
        """
        raise NotImplementedError

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        """Query the status of an order.

        Args:
            exchange_order_id: ID returned by place_order.
            market_id: Market identifier.

        Returns:
            OrderStatusResponse with current state (open, filled, cancelled, etc.).
        """
        raise NotImplementedError

    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """List all open orders, optionally filtered by market.

        Args:
            market_ids: If provided, only return orders for these markets.

        Returns:
            List of OpenOrder objects.
        """
        raise NotImplementedError

    async def verify_connectivity(self) -> bool:
        """Check if the venue API is reachable and authenticated.

        Returns:
            True if connection is healthy.
        """
        raise NotImplementedError

    async def get_market_info(self, market_id: str) -> Dict[str, Any]:
        """Fetch market metadata (resolution date, outcomes, etc.).

        Args:
            market_id: Market identifier.

        Returns:
            Dict with market details.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Clean up resources (sessions, connections, etc.)."""
        pass
