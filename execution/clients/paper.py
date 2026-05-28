"""execution/clients/paper.py — Paper trading client for safe live-pipeline testing.

Simulates exchange responses using real market data but fake order execution.
Uses realistic fill probabilities based on order price vs current mid price.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional

from execution.engine import (
    ExchangeClient,
    OpenOrder,
    OrderStatusResponse,
    OrderStatusFill,
    PlacedFill,
    PlacedOrderResponse,
)
from execution.models import OrderSubmission
from src.types import Platform

logger = logging.getLogger(__name__)


class PaperTradingClient:
    """
    Paper trading client that simulates order execution.

    Orders are accepted but fills are simulated based on:
    - Price aggressiveness vs current mid
    - Random fill probability (70-95% for aggressive orders)
    - Simulated latency (50-200ms)

    Safe for testing the full live pipeline without real capital at risk.
    """

    PLATFORM: Platform = Platform.POLYMARKET

    def __init__(
        self,
        fill_probability: float = 0.85,
        latency_ms_range: tuple[int, int] = (50, 200),
        seed: Optional[int] = None,
    ) -> None:
        self._fill_probability = fill_probability
        self._latency_ms_range = latency_ms_range
        self._rng = random.Random(seed)
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._reported_status_fills: Dict[str, float] = {}
        self._session_active = False

        logger.info(
            "PaperTradingClient initialized: fill_prob=%.2f, latency=%d-%dms",
            fill_probability, latency_ms_range[0], latency_ms_range[1],
        )

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse:
        """Simulate order placement with realistic fill behavior."""
        exchange_order_id = f"paper-{submission.proposal_id}"
        self._orders[exchange_order_id] = {
            "submission": submission,
            "effective_price": effective_price,
            "status": "live",
            "fills": [],
            "filled_usdc": 0.0,
            "placed_at": int(time.time() * 1000),
        }

        latency = self._rng.randint(*self._latency_ms_range)
        logger.info(
            "PAPER ORDER: %s %s %s @ %.4f size=$%.2f (simulated latency=%dms)",
            exchange_order_id[:16],
            submission.side.value,
            submission.market_id,
            submission.limit_price,
            submission.size_usdc,
            latency,
        )

        fill_probability = self._compute_fill_probability(submission, effective_price)
        if self._rng.random() < fill_probability:
            fill_ratio = self._rng.uniform(0.5, 1.0)
            fill_usdc = round(submission.size_usdc * fill_ratio, 2)
            fill_price = submission.limit_price
            fill_tokens = fill_usdc / fill_price if fill_price > 0 else 0

            fill = PlacedFill(
                fill_usdc=fill_usdc,
                fill_price=fill_price,
                fill_tokens=fill_tokens,
                ts=int(time.time() * 1000),
            )
            self._orders[exchange_order_id]["fills"].append(fill)
            self._orders[exchange_order_id]["filled_usdc"] = fill_usdc
            self._orders[exchange_order_id]["status"] = "matched"
            self._reported_status_fills[exchange_order_id] = fill_usdc

            logger.info(
                "PAPER FILL: %s filled $%.2f/%.2f (%.0f%%)",
                exchange_order_id[:16],
                fill_usdc,
                submission.size_usdc,
                fill_ratio * 100,
            )

        return PlacedOrderResponse(
            exchange_order_id=exchange_order_id,
            status=self._orders[exchange_order_id]["status"],
            fills=self._orders[exchange_order_id]["fills"],
        )

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool:
        """Simulate order cancellation."""
        order = self._orders.get(exchange_order_id)
        if order is None:
            return True
        if order["status"] in ("cancelled", "matched"):
            return True
        order["status"] = "cancelled"
        logger.info("PAPER CANCEL: %s", exchange_order_id[:16])
        return True

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse:
        """Return simulated order status."""
        order = self._orders.get(exchange_order_id)
        if order is None:
            return OrderStatusResponse(
                exchange_order_id=exchange_order_id,
                is_live=False,
                is_cancelled=True,
                is_filled=False,
                remaining_usdc=0.0,
            )

        status = order["status"]
        filled_usdc = order["filled_usdc"]
        reported = self._reported_status_fills.get(exchange_order_id, 0.0)
        delta = max(0.0, filled_usdc - reported)
        new_fills = []
        if delta > 0:
            fill_price = order["submission"].limit_price
            new_fills.append(OrderStatusFill(
                fill_usdc=delta,
                fill_price=fill_price,
                fill_tokens=delta / fill_price if fill_price > 0 else 0.0,
                ts=int(time.time() * 1000),
            ))
            self._reported_status_fills[exchange_order_id] = filled_usdc
        return OrderStatusResponse(
            exchange_order_id=exchange_order_id,
            is_live=status == "live",
            is_cancelled=status == "cancelled",
            is_filled=status == "matched",
            remaining_usdc=order["submission"].size_usdc - order["filled_usdc"],
            new_fills=new_fills,
        )

    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """Return simulated open orders."""
        open_orders = []
        for exch_id, order in self._orders.items():
            if order["status"] != "live":
                continue
            if market_ids and order["submission"].market_id not in market_ids:
                continue
            open_orders.append(OpenOrder(
                exchange_order_id=exch_id,
                market_id=order["submission"].market_id,
                side=order["submission"].side.value,
                size_usdc=order["submission"].size_usdc,
                filled_usdc=order["filled_usdc"],
                limit_price=order["submission"].limit_price,
                ts=order["placed_at"],
            ))
        return open_orders

    async def verify_connectivity(self) -> bool:
        """Paper trading is always connected."""
        return True

    async def close(self) -> None:
        """Clear simulated state."""
        self._orders.clear()
        self._reported_status_fills.clear()
        self._session_active = False

    def _compute_fill_probability(
        self, submission: OrderSubmission, effective_price: float
    ) -> float:
        """Compute fill probability based on order aggressiveness."""
        base_prob = self._fill_probability

        if submission.strategy_id.value == "ARB":
            return min(0.95, base_prob + 0.10)

        mid_price = effective_price
        limit_price = submission.limit_price

        if limit_price <= 0 or mid_price <= 0:
            return base_prob * 0.5

        price_ratio = limit_price / mid_price

        if submission.side.is_buy:
            aggressiveness = price_ratio - 1.0
        else:
            aggressiveness = 1.0 - price_ratio

        if aggressiveness >= 0:
            return min(0.95, base_prob + aggressiveness * 2)
        else:
            return max(0.1, base_prob + aggressiveness * 3)
