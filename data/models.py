"""
data/models.py — Market data and feature vector models.

Uses stdlib dataclasses instead of pydantic — zero external dependencies.
Provides model_copy() and model_dump() for API compatibility.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

from src.enums import Platform
from src.errors import CrossedBookError


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _model_copy(instance: Any, update: Optional[dict[str, Any]] = None) -> Any:
    """Return a new instance with updated fields (mirroring pydantic's API)."""
    d = asdict(instance)
    if update:
        d.update(update)
    return instance.__class__(**d)


def _model_dump(instance: Any) -> dict[str, Any]:
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

    def __post_init__(self) -> None:
        if self.yes_bid >= self.yes_ask:
            raise CrossedBookError(
                f"Crossed YES book on {self.market_id}: "
                f"bid={self.yes_bid} >= ask={self.yes_ask}",
                market_id=self.market_id,
                platform=self.platform.value,
            )
        if self.no_bid >= self.no_ask:
            raise CrossedBookError(
                f"Crossed NO book on {self.market_id}: "
                f"bid={self.no_bid} >= ask={self.no_ask}",
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

    def model_copy(self, update: Optional[dict[str, Any]] = None) -> "MarketSnapshot":
        d = asdict(self)
        if update:
            d.update(update)
        return self.__class__(**d)

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# VenueSnapshot — per-venue derived data inside FeatureVector
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VenueSnapshot:
    """Derived per-venue snapshot inside FeatureVector.venues."""
    mid:        float
    spread:     float
    ofi:        float
    bid_depth:  float
    ask_depth:  float


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

    # Per-venue derived snapshots (keyed by Platform)
    venues:          Dict[Platform, VenueSnapshot]

    # Volatility (None during warm-up)
    vol_30s:   Optional[float]

    # Market lifecycle
    days_to_resolution: Optional[float]

    # Portfolio context
    portfolio_delta: float

    # Volatility regime (None during warm-up)
    vol_regime: Optional[str] = None

    # Cross-market correlation (market_id → correlation, empty dict when unavailable)
    correlations: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if math.isnan(self.arb_signal) and not self.stale_markets:
            raise ValueError("stale_markets must be non-empty when arb_signal is NaN")
        if not math.isnan(self.arb_signal) and self.stale_markets:
            raise ValueError("stale_markets must be empty when arb_signal is a valid number")

    @property
    def arb_tradeable(self) -> bool:
        return not math.isnan(self.arb_signal) and self.arb_signal > 0.0

    def model_copy(self, update: Optional[dict[str, Any]] = None) -> "FeatureVector":
        d = asdict(self)
        if update:
            d.update(update)
        return self.__class__(**d)

    def model_dump(self) -> dict[str, Any]:
        d = asdict(self)
        d["stale_markets"] = [p.value for p in self.stale_markets]
        d["venues"] = {p.value: dict(v) for p, v in d["venues"].items()}
        return d