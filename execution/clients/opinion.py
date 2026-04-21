"""execution/clients/opinion.py — Opinion Markets ExchangeClient structure."""
from __future__ import annotations

import logging
import aiohttp
from typing import Optional, List

from execution.engine import (
    ExchangeClient,
    PlacedOrderResponse,
    OrderStatusResponse,
)
from execution.models import OrderSubmission
from src.types import Platform

logger = logging.getLogger(__name__)


class OpinionClient:
    """
    Opinion Markets REST API Client.
    
    IMPLEMENTATION REQUIRED: depends on official API documentation
    """
    PLATFORM = Platform.OPINION

    def __init__(
        self,
        api_key: str,
        host: str = "UNKNOWN_IMPLEMENTATION_REQUIRED",
    ) -> None:
        self._api_key = api_key
        self._host = host
        self._session: Optional[aiohttp.ClientSession] = None
        # TODO: Initialize other required state

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        IMPLEMENTATION REQUIRED: depends on official API documentation.
        - Return an aiohttp session configured with the correct authentication headers.
        """
        if self._session is None or self._session.closed:
            # TODO: Add exact auth headers required by Opinion Markets
            headers = {"Authorization": "UNKNOWN_IMPLEMENTATION_REQUIRED"}
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def place_order(
        self, submission: OrderSubmission, effective_price: float
    ) -> PlacedOrderResponse:
        """
        Submits an order to the Opinion exchange.
        
        IMPLEMENTATION REQUIRED: depends on official API documentation.
        - Must map `submission` to Opinion's expected JSON payload format.
        - Must return a `PlacedOrderResponse` or raise `ExchangeRejected`.
        """
        # TODO: Implement API call
        raise NotImplementedError("IMPLEMENTATION REQUIRED: depends on official API documentation")

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """
        Cancels an active order on the Opinion exchange.
        
        IMPLEMENTATION REQUIRED: depends on official API documentation.
        - Must return True if cancelled or not found, False otherwise.
        """
        # TODO: Implement API call
        raise NotImplementedError("IMPLEMENTATION REQUIRED: depends on official API documentation")

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        """
        Fetches the current status and fills for a given order ID.
        
        IMPLEMENTATION REQUIRED: depends on official API documentation.
        - Must parse raw response into `OrderStatusResponse` and `OrderStatusFill`.
        """
        # TODO: Implement API call
        raise NotImplementedError("IMPLEMENTATION REQUIRED: depends on official API documentation")
