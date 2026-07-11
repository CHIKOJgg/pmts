from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class PositionSizingOptimizer:
    def __init__(self, max_total_exposure_usdc: float = 10_000.0) -> None:
        self._max_total = max_total_exposure_usdc
        self._market_limits: Dict[str, float] = {}

    def set_market_limit(self, market_id: str, max_exposure_usdc: float) -> None:
        self._market_limits[market_id] = max_exposure_usdc

    def max_order_size(
        self,
        market_id: str,
        arb_signal: float,
        correlations: Optional[Dict[str, float]] = None,
        current_exposure: float = 0.0,
        portfolio_equity: float = 10_000.0,
    ) -> float:
        market_cap = self._market_limits.get(market_id, self._max_total * 0.2)

        if current_exposure >= market_cap:
            return 0.0

        available = market_cap - current_exposure

        if correlations:
            avg_corr = sum(abs(v) for v in correlations.values()) / max(len(correlations), 1)
            if avg_corr > 0.7:
                available *= 0.5
            elif avg_corr > 0.4:
                available *= 0.75

        signal_strength = min(abs(arb_signal) * 100, 1.0)
        available *= max(signal_strength, 0.1)

        portfolio_max = portfolio_equity * 0.05
        return min(available, portfolio_max)
