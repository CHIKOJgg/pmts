"""strategies/correlation.py — Cross-market correlation analysis.

Uses lazy caching: when a market's price updates, only correlations involving
that market are recomputed on the next request (O(N) instead of O(N²) per tick).
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Dict, Set

logger = logging.getLogger(__name__)


class CorrelationTracker:
    """Tracks price correlations between markets with lazy recomputation."""

    def __init__(self, window_size: int = 100) -> None:
        self._window = window_size
        self._prices: Dict[str, deque[float]] = {}
        self._cache: Dict[str, Dict[str, float]] = {}
        self._dirty: Set[str] = set()

    def update(self, market_id: str, mid_price: float) -> None:
        if market_id not in self._prices:
            self._prices[market_id] = deque(maxlen=self._window)
        self._prices[market_id].append(mid_price)
        self._dirty.add(market_id)

    def get_correlation(self, market_a: str, market_b: str) -> float:
        if market_a not in self._prices or market_b not in self._prices:
            return 0.0
        if market_a in self._cache and market_b in self._cache[market_a] and market_a not in self._dirty and market_b not in self._dirty:
            return self._cache[market_a][market_b]
        return self._compute(market_a, market_b)

    def get_all_correlations(self, market_id: str) -> Dict[str, float]:
        if market_id not in self._prices:
            return {}
        if market_id in self._dirty:
            result: Dict[str, float] = {}
            for other in self._prices:
                if other != market_id:
                    result[other] = self._compute(market_id, other)
            self._cache[market_id] = result
            self._dirty.discard(market_id)
        return dict(self._cache.get(market_id, {}))

    def _compute(self, market_a: str, market_b: str) -> float:
        prices_a = list(self._prices[market_a])
        prices_b = list(self._prices[market_b])
        min_len = min(len(prices_a), len(prices_b))
        if min_len < 10:
            return 0.0
        prices_a = prices_a[-min_len:]
        prices_b = prices_b[-min_len:]
        mean_a = sum(prices_a) / min_len
        mean_b = sum(prices_b) / min_len
        cov = float(sum((a - mean_a) * (b - mean_b) for a, b in zip(prices_a, prices_b))) / (min_len - 1)
        std_a = math.sqrt(float(sum((a - mean_a) ** 2 for a in prices_a) / (min_len - 1)))
        std_b = math.sqrt(float(sum((b - mean_b) ** 2 for b in prices_b) / (min_len - 1)))
        if std_a < 1e-9 or std_b < 1e-9:
            return 0.0
        return cov / (std_a * std_b)
