"""strategies/correlation.py — Cross-market correlation analysis."""
from __future__ import annotations

import logging
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CorrelationTracker:
    """Tracks price correlations between markets."""

    def __init__(self, window_size: int = 100) -> None:
        self._window = window_size
        self._prices: Dict[str, deque[float]] = {}

    def update(self, market_id: str, mid_price: float) -> None:
        if market_id not in self._prices:
            self._prices[market_id] = deque(maxlen=self._window)
        self._prices[market_id].append(mid_price)

    def get_correlation(self, market_a: str, market_b: str) -> float:
        if market_a not in self._prices or market_b not in self._prices:
            return 0.0
        prices_a = list(self._prices[market_a])
        prices_b = list(self._prices[market_b])
        min_len = min(len(prices_a), len(prices_b))
        if min_len < 10:
            return 0.0
        prices_a = prices_a[-min_len:]
        prices_b = prices_b[-min_len:]
        mean_a = sum(prices_a) / min_len
        mean_b = sum(prices_b) / min_len
        cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(prices_a, prices_b)) / min_len
        std_a = (sum((a - mean_a) ** 2 for a in prices_a) / min_len) ** 0.5
        std_b = (sum((b - mean_b) ** 2 for b in prices_b) / min_len) ** 0.5
        if std_a < 1e-9 or std_b < 1e-9:
            return 0.0
        return cov / (std_a * std_b)

    def get_all_correlations(self, market_id: str) -> Dict[str, float]:
        return {
            other: self.get_correlation(market_id, other)
            for other in self._prices
            if other != market_id
        }
