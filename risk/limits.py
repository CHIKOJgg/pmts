"""risk/limits.py — All risk thresholds in one validated dataclass."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    drawdown_kill_pct:        float = 0.20
    drawdown_warn_pct:        float = 0.15
    max_market_exposure_pct:  float = 0.05
    max_market_exposure_usdc: float = 500.0
    max_arb_capital_usdc:     float = 2_000.0
    max_mm_capital_usdc:      float = 3_000.0
    max_net_delta_per_market: float = 50.0
    delta_hedge_threshold:    float = 20.0
    max_single_order_usdc:    float = 200.0
    min_single_order_usdc:    float = 1.0
    min_free_capital_pct:     float = 0.10
    dedup_window_s:           int   = 60
    dedup_cache_size:         int   = 10_000
    max_mtm_age_ms:           int   = 10_000
    session_loss_limit_usdc:  float = 500.0
    kill_switch_grace_s:      float = 5.0
    soft_kill_on_drawdown:    bool  = True

    def __post_init__(self) -> None:
        errs = []
        if not (0 < self.drawdown_warn_pct < self.drawdown_kill_pct <= 1.0):
            errs.append(
                f"drawdown_warn_pct ({self.drawdown_warn_pct}) must be < "
                f"drawdown_kill_pct ({self.drawdown_kill_pct})"
            )
        if self.delta_hedge_threshold >= self.max_net_delta_per_market:
            errs.append("delta_hedge_threshold must be < max_net_delta_per_market")
        if self.min_single_order_usdc >= self.max_single_order_usdc:
            errs.append("min_single_order_usdc must be < max_single_order_usdc")
        if not (0.0 <= self.min_free_capital_pct < 1.0):
            errs.append("min_free_capital_pct must be in [0, 1)")
        if self.max_mtm_age_ms <= 0:
            errs.append("max_mtm_age_ms must be > 0")
        if errs:
            raise ValueError("Invalid RiskLimits:\n" + "\n".join(f"  {e}" for e in errs))


DEFAULT_LIMITS = RiskLimits()
