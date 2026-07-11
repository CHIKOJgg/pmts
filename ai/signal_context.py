"""ai/signal_context.py — SignalContext: the ONLY output the AI module produces."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

CONFIDENCE_MIN: float = 0.10
CONFIDENCE_MAX: float = 2.00
URGENT_HEDGE:   float = 0.80


class MarketRegime(str, Enum):
    TRENDING       = "trending"
    MEAN_REVERTING = "mean_reverting"
    VOLATILE       = "volatile"
    THIN           = "thin"
    STABLE         = "stable"
    UNKNOWN        = "unknown"


class VolRegime(str, Enum):
    LOW    = "low"
    NORMAL = "normal"
    HIGH   = "high"
    SPIKE  = "spike"


@dataclass(frozen=True)
class SignalContext:
    """
    AI enrichment output. Contains regime labels and confidence modifiers ONLY.
    No order-routing fields are present or permitted.

    What StrategyEngine can do with this:
      - confidence_multiplier: scale the arb min_net_edge threshold
      - suppress_mm: skip MM quoting for this tick on this market
      - arb_quality: additional modifier on the arb threshold
      - hedge_urgency: override DeltaNeutral cooldown if above URGENT_HEDGE

    What the AI module CANNOT do:
      - Specify price, size, side, platform, or expiry
      - Access RiskEngine, ExecutionEngine, or PortfolioManager
    """
    market_id:            str
    confidence_multiplier: float   # [CONFIDENCE_MIN, CONFIDENCE_MAX]
    regime:               MarketRegime
    vol_regime:           VolRegime
    suppress_mm:          bool
    arb_quality:          float    # [0.0, 1.0]
    hedge_urgency:        float    # [0.0, 1.0]
    model_version:        str
    inference_ms:         float
    feature_count:        int
    is_fallback:          bool

    def __post_init__(self) -> None:
        if not (CONFIDENCE_MIN <= self.confidence_multiplier <= CONFIDENCE_MAX):
            raise ValueError(
                f"confidence_multiplier {self.confidence_multiplier} "
                f"out of [{CONFIDENCE_MIN}, {CONFIDENCE_MAX}]"
            )
        if not (0.0 <= self.arb_quality <= 1.0):
            raise ValueError(f"arb_quality must be in [0, 1], got {self.arb_quality}")
        if not (0.0 <= self.hedge_urgency <= 1.0):
            raise ValueError(f"hedge_urgency must be in [0, 1], got {self.hedge_urgency}")
        # Cannot suppress BOTH arb and MM (that would bypass the kill switch)
        if (self.suppress_mm and self.arb_quality < 0.01
                and self.confidence_multiplier <= CONFIDENCE_MIN):
            raise ValueError(
                "Cannot suppress both MM (suppress_mm=True) and arb "
                "(arb_quality≈0, confidence≈MIN) simultaneously — "
                "use the kill switch for a full trading halt."
            )

    @property
    def is_urgent_hedge(self) -> bool:
        return self.hedge_urgency >= URGENT_HEDGE

    @property
    def effective_arb_multiplier(self) -> float:
        """Combined modifier applied to arb edge threshold."""
        return max(0.1, self.arb_quality * self.confidence_multiplier)

    @property
    def confidence_adjustment(self) -> float:
        """Convenience delta used to scale arb thresholds from the neutral baseline."""
        return self.confidence_multiplier - 1.0


# Neutral sentinel — no AI adjustment
NEUTRAL_CONTEXT = SignalContext(
    market_id="",
    confidence_multiplier=1.0,
    regime=MarketRegime.UNKNOWN,
    vol_regime=VolRegime.NORMAL,
    suppress_mm=False,
    arb_quality=0.5,
    hedge_urgency=0.0,
    model_version="neutral-v0",
    inference_ms=0.0,
    feature_count=0,
    is_fallback=True,
)
