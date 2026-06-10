"""
ai/heuristic.py — Rule-based signal enhancement.

Zero external dependencies. Never fails. Used when:
  - AI is disabled in config
  - Claude API times out or errors
  - After MAX_CONSECUTIVE_ERRORS API failures
"""

from __future__ import annotations

import logging
import math

from ai.signal_context import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    MarketRegime,
    SignalContext,
    VolRegime,
)
from data.models import FeatureVector
from src.enums import Platform

logger = logging.getLogger(__name__)

_SPREAD_THIN = 0.08  # spread/ask > 8% → THIN
_VOL_HIGH = 0.015  # vol_30s > 1.5% → HIGH
_VOL_SPIKE = 0.040  # vol_30s > 4.0% → SPIKE
_OFI_TREND = 0.50  # |OFI| > 0.5 → TRENDING
_DAYS_NO_MM = 3.0  # suppress MM within this many days of resolution
_DELTA_SOFT = 15.0  # start raising hedge_urgency above this |delta|
_DELTA_HARD = 40.0  # hedge_urgency=1.0 at this |delta|


def heuristic_enhance(fv: FeatureVector) -> SignalContext:
    """
    Apply rule-based signal enhancement. Always returns a valid SignalContext.
    O(1), no I/O, no side effects.
    """
    vol = fv.vol_30s if fv.vol_30s is not None else 0.0
    pm_v = fv.venues.get(Platform.POLYMARKET)
    op_v = fv.venues.get(Platform.OPINION)

    # ── Vol regime ────────────────────────────────────────────────────────────
    if vol >= _VOL_SPIKE:
        vol_regime = VolRegime.SPIKE
    elif vol >= _VOL_HIGH:
        vol_regime = VolRegime.HIGH
    elif vol < _VOL_HIGH / 4.0:
        vol_regime = VolRegime.LOW
    else:
        vol_regime = VolRegime.NORMAL

    # ── Market regime ─────────────────────────────────────────────────────────
    ofi_pm = pm_v.ofi if pm_v else 0.0
    ofi_op = op_v.ofi if op_v else 0.0
    spread_pm = pm_v.spread if pm_v else 0.0
    spread_op = op_v.spread if op_v else 0.0
    mid_pm = pm_v.mid if pm_v else 0.5
    mid_op = op_v.mid if op_v else 0.5
    ask_depth_pm = pm_v.ask_depth if pm_v else 0.0
    ask_depth_op = op_v.ask_depth if op_v else 0.0

    ofi_mag = (abs(ofi_pm) + abs(ofi_op)) / 2.0
    safe_pm = max(0.001, mid_pm + spread_pm / 2)
    safe_op = max(0.001, mid_op + spread_op / 2)
    spr_frac = max(spread_pm / safe_pm, spread_op / safe_op)

    if spr_frac > _SPREAD_THIN:
        regime = MarketRegime.THIN
    elif vol_regime == VolRegime.SPIKE:
        regime = MarketRegime.VOLATILE
    elif ofi_mag > _OFI_TREND:
        regime = MarketRegime.TRENDING
    elif vol_regime == VolRegime.LOW and spr_frac < 0.03:
        regime = MarketRegime.STABLE
    else:
        regime = MarketRegime.MEAN_REVERTING

    # ── Arb quality ───────────────────────────────────────────────────────────
    if math.isnan(fv.arb_signal) or fv.arb_signal <= 0:
        arb_quality = 0.0
    else:
        base = min(1.0, fv.arb_signal / 0.03)
        spr_penalty = max(0.0, 1.0 - (spread_pm + spread_op) / 2.0 / 0.04)
        min_depth = min(ask_depth_pm, ask_depth_op)
        depth_score = min(1.0, math.log1p(min_depth) / math.log1p(1000.0))
        ofi_adv = max(0.0, -(ofi_pm + ofi_op) / 2.0)
        ofi_score = 1.0 - ofi_adv * 0.5
        regime_f = {
            MarketRegime.STABLE: 1.0,
            MarketRegime.MEAN_REVERTING: 0.9,
            MarketRegime.TRENDING: 0.7,
            MarketRegime.VOLATILE: 0.5,
            MarketRegime.THIN: 0.2,
            MarketRegime.UNKNOWN: 0.6,
        }.get(regime, 0.6)
        arb_quality = max(0.0, min(1.0, base * spr_penalty * depth_score * ofi_score * regime_f))

    # ── Confidence multiplier ─────────────────────────────────────────────────
    conf = {
        MarketRegime.STABLE: 1.30,
        MarketRegime.MEAN_REVERTING: 1.10,
        MarketRegime.TRENDING: 0.75,
        MarketRegime.VOLATILE: 0.60,
        MarketRegime.THIN: 0.40,
        MarketRegime.UNKNOWN: 1.00,
    }.get(regime, 1.0)
    conf *= {
        VolRegime.LOW: 1.15,
        VolRegime.NORMAL: 1.00,
        VolRegime.HIGH: 0.70,
        VolRegime.SPIKE: 0.30,
    }.get(vol_regime, 1.0)
    # Penalise conflicting OFI across venues
    if (ofi_pm > 0.2 and ofi_op < -0.2) or (ofi_pm < -0.2 and ofi_op > 0.2):
        conf *= 0.80
    # Penalise near-expiry markets
    days = fv.days_to_resolution
    if days is not None and days < 5.0:
        conf *= max(0.3, days / 5.0)
    conf = max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, conf))

    # ── MM suppression ────────────────────────────────────────────────────────
    suppress_mm = (
        (days is not None and days <= _DAYS_NO_MM) or vol_regime == VolRegime.SPIKE or regime == MarketRegime.THIN
    )

    # Safety: cannot suppress both arb and MM simultaneously
    if suppress_mm and arb_quality < 0.01:
        # If both would be suppressed, force at least one enabled
        if conf <= CONFIDENCE_MIN:
            arb_quality = 0.05
            suppress_mm = False  # Must allow arbitrage
        else:
            # Allow MM but raise confidence to make it viable
            suppress_mm = False

    # Additional check: ensure at least one strategy is enabled
    if suppress_mm and arb_quality < 0.01:
        logger.warning("AI/Heuristic produced total blackout - forcing minimal arb viability")
        arb_quality = max(0.05, arb_quality)
        suppress_mm = False

    # ── Hedge urgency ─────────────────────────────────────────────────────────
    ad = abs(fv.portfolio_delta)
    if ad <= _DELTA_SOFT:
        base_urg = 0.0
    elif ad >= _DELTA_HARD:
        base_urg = 1.0
    else:
        base_urg = (ad - _DELTA_SOFT) / (_DELTA_HARD - _DELTA_SOFT)
    vol_amp = {
        VolRegime.LOW: 0.7,
        VolRegime.NORMAL: 1.0,
        VolRegime.HIGH: 1.3,
        VolRegime.SPIKE: 1.6,
    }.get(vol_regime, 1.0)
    hedge_urgency = max(0.0, min(1.0, base_urg * vol_amp))

    return SignalContext(
        market_id=fv.market_id,
        confidence_multiplier=conf,
        regime=regime,
        vol_regime=vol_regime,
        suppress_mm=suppress_mm,
        arb_quality=arb_quality,
        hedge_urgency=hedge_urgency,
        model_version="heuristic-v1",
        inference_ms=0.0,
        feature_count=20,
        is_fallback=True,
    )
