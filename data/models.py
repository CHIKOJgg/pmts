"""
data/models.py — Market data and feature vector models.

Uses stdlib dataclasses instead of pydantic — zero external dependencies.
Provides model_copy() and model_dump() for API compatibility.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import List, Optional

from src.types import Platform
from src.errors import CrossedBookError


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _model_copy(instance, update: dict = None):
    """Return a new instance with updated fields (mirroring pydantic's API)."""
    d = asdict(instance)
    if update:
        d.update(update)
    return instance.__class__(**d)


def _model_dump(instance) -> dict:
    """Return a plain dict (mirroring pydantic's API)."""
    return asdict(instance)


# ─────────────────────────────────────────────────────────────────────────────
# MarketSnapshot
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketSnapshot:
    """Immutable point-in-time view of one market on one venue."""
    market_id:      str
    platform:       Platform
    yes_bid:        float
    yes_ask:        float
    no_bid:         float
    no_ask:         float
    bid_depth_usdc: float
    ask_depth_usdc: float
    taker_fee_bps:  int
    ts:             int       # exchange timestamp, epoch ms
    received_ts:    int       # when we received it, epoch ms
    is_stale:       bool = False
    days_to_resolution: Optional[float] = None

    def __post_init__(self):
        if self.yes_bid >= self.yes_ask:
            raise CrossedBookError(
                f"Crossed YES book on {self.market_id}: "
                f"bid={self.yes_bid} >= ask={self.yes_ask}",
                market_id=self.market_id,
                platform=self.platform.value,
            )

    # ── Computed properties ──────────────────────────────────────────────────

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def no_mid(self) -> float:
        return (self.no_bid + self.no_ask) / 2.0

    @property
    def yes_spread(self) -> float:
        return self.yes_ask - self.yes_bid

    @property
    def no_spread(self) -> float:
        return self.no_ask - self.no_bid

    @property
    def taker_fee(self) -> float:
        return self.taker_fee_bps / 10_000.0

    # ── Pydantic-compatible helpers ──────────────────────────────────────────

    def model_copy(self, update: dict = None) -> "MarketSnapshot":
        return _model_copy(self, update)

    def model_dump(self) -> dict:
        return _model_dump(self)


# ─────────────────────────────────────────────────────────────────────────────
# FeatureVector
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeatureVector:
    """Derived signal snapshot produced by FeatureEngine."""
    market_id:       str
    ts:              int    # source snapshot timestamp
    computed_ts:     int    # when FE computed this vector

    # Arb signal (NaN when stale)
    arb_signal:      float
    stale_markets:   List[Platform]  # populated when arb_signal is NaN

    # Per-venue mid & spread
    mid_pm:    float
    mid_op:    float
    spread_pm: float
    spread_op: float

    # Order-flow imbalance [-1, 1]
    ofi_pm:    float
    ofi_op:    float

    # Volatility (None during warm-up)
    vol_30s:   Optional[float]

    # Market lifecycle
    days_to_resolution: Optional[float]

    # Portfolio context
    portfolio_delta: float

    # Book depth
    bid_depth_pm: float
    ask_depth_pm: float
    bid_depth_op: float
    ask_depth_op: float

    def __post_init__(self):
        if math.isnan(self.arb_signal) and not self.stale_markets:
            raise ValueError("stale_markets must be non-empty when arb_signal is NaN")
        if not math.isnan(self.arb_signal) and self.stale_markets:
            raise ValueError("stale_markets must be empty when arb_signal is a valid number")

    @property
    def arb_tradeable(self) -> bool:
        return not math.isnan(self.arb_signal) and self.arb_signal > 0.0

    def model_dump(self) -> dict:
        d = _model_dump(self)
        d["stale_markets"] = [p.value for p in self.stale_markets]
        return d