"""strategies/arbitrage.py — N-venue arbitrage with strict feasibility checks."""

from __future__ import annotations

import itertools
import logging
import math
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from ai.signal_context import SignalContext
from data.models import FeatureVector, VenueSnapshot
from execution.models import OrderProposal
from src.clock import Clock, LiveClock
from src.enums import ArbLeg, OrderType, Platform, Side, StrategyId

logger = logging.getLogger(__name__)

# Cost model constants
IMPACT_FACTOR: float = 0.018  # sqrt-impact; calibrated to thin prediction market books
OFI_ADVERSE_THRESHOLD: float = 0.25  # OFI above this → adversity premium
OFI_ADVERSE_MULT: float = 1.60  # impact multiplier when OFI adverse
MIN_DEPTH_USDC: float = 10.0  # minimum depth for reliable cost estimate
FILL_CERTAINTY: float = 0.50  # prediction market books are thinner than displayed

ARB_EXPIRY_MS: int = 3_000  # 3-second deadline for both legs (accounts for cross-venue latency)

# Default fees by platform (via ArbConfig)
DEFAULT_FEES: Dict[Platform, int] = {
    Platform.POLYMARKET: 20,
    Platform.OPINION: 25,
}


# ─────────────────────────────────────────────────────────────────────────────
# Cost estimate
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CostEstimate:
    fee_usdc: float
    spread_usdc: float
    impact_usdc: float

    @property
    def total(self) -> float:
        return self.fee_usdc + self.spread_usdc + self.impact_usdc

    def as_fraction(self, size_usdc: float) -> float:
        return self.total / size_usdc if size_usdc > 0 else 0.0


def estimate_taker_cost(
    size_usdc: float,
    ask_price: float,
    bid_price: float,
    depth_usdc: float,
    taker_fee_bps: int,
    ofi: float = 0.0,
) -> CostEstimate:
    """Estimate cost of a taker order using actual ask price (not mid)."""
    fee_usdc = size_usdc * taker_fee_bps / 10_000

    spread = max(0.0, ask_price - bid_price)
    safe_ask = max(0.01, ask_price)
    spread_usdc = size_usdc * (spread / 2) / safe_ask

    safe_depth = max(MIN_DEPTH_USDC, depth_usdc)
    impact_frac = IMPACT_FACTOR * math.sqrt(size_usdc / safe_depth)
    if abs(ofi) > OFI_ADVERSE_THRESHOLD:
        impact_frac *= OFI_ADVERSE_MULT
    impact_usdc = size_usdc * impact_frac

    return CostEstimate(fee_usdc=fee_usdc, spread_usdc=spread_usdc, impact_usdc=impact_usdc)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArbConfig:
    min_net_edge: float = 0.006
    max_spread_fraction: float = 0.07
    fill_certainty: float = FILL_CERTAINTY
    min_fill_ratio: float = 0.80
    max_order_usdc: float = 200.0
    min_order_usdc: float = 5.0
    max_signal_age_ms: int = 1000  # 1 second — accounts for WS latency + pipeline stages
    arb_expiry_ms: int = ARB_EXPIRY_MS
    fees: Dict[Platform, int] = None  # per-platform fee in bps
    min_days_to_resolution: float = 0.0
    ofi_adverse_threshold: float = OFI_ADVERSE_THRESHOLD
    ofi_adverse_mult: float = OFI_ADVERSE_MULT

    def __post_init__(self):
        if self.fees is None:
            object.__setattr__(self, "fees", dict(DEFAULT_FEES))


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ArbEvaluation:
    market_id: str
    evaluated_at: int
    signal_age_ms: int
    arb_signal: float
    accepted: bool
    rejection_reason: Optional[str]
    direction: Optional[str] = None
    pair: str = ""
    leg1_cost_frac: Optional[float] = None
    leg2_cost_frac: Optional[float] = None
    net_edge: Optional[float] = None
    raw_size_usdc: Optional[float] = None
    final_size_usdc: Optional[float] = None
    leg1_proposal: Optional[OrderProposal] = None
    leg2_proposal: Optional[OrderProposal] = None


# ─────────────────────────────────────────────────────────────────────────────
# ArbitrageStrategy
# ─────────────────────────────────────────────────────────────────────────────


class ArbitrageStrategy:
    """
    N-venue stateless evaluator. Call evaluate(fv) every tick.

    Enumerates all venue pairs (N×(N-1)/2), tries both directions for each,
    and returns the best accepted opportunity.  Returns ArbEvaluation.
    """

    def __init__(
        self,
        config: ArbConfig = ArbConfig(),
        clock: Clock = LiveClock(),
    ) -> None:
        self._cfg = config
        self._clock: Clock = clock

        self.evaluated: int = 0
        self.accepted: int = 0
        self.rejected_stale: int = 0
        self.rejected_no_edge: int = 0
        self.rejected_spread: int = 0
        self.rejected_depth: int = 0

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fee_bps(self, platform: Platform) -> int:
        return self._cfg.fees.get(platform, 0)

    def _adjust_for_ofi(self, raw_edge: float, ofi_a: float, ofi_b: float) -> float:
        ofi_net = ofi_a - ofi_b
        if abs(ofi_net) > self._cfg.ofi_adverse_threshold:
            penalty = 1 + (abs(ofi_net) - self._cfg.ofi_adverse_threshold) * self._cfg.ofi_adverse_mult
            return raw_edge / penalty
        return raw_edge

    def _dynamic_min_edge(self, vol_regime: Optional[str]) -> float:
        if vol_regime is None:
            return self._cfg.min_net_edge
        return {
            "LOW":    self._cfg.min_net_edge * 1.5,
            "NORMAL": self._cfg.min_net_edge,
            "HIGH":   self._cfg.min_net_edge * 0.8,
            "SPIKE":  self._cfg.min_net_edge * 0.7,
        }.get(vol_regime, self._cfg.min_net_edge)

    def _evaluate_pair(
        self, fv: FeatureVector, now: int, signal_age_ms: int, ctx: Optional[SignalContext],
        plat_a: Platform, v_a: VenueSnapshot, plat_b: Platform, v_b: VenueSnapshot,
    ) -> ArbEvaluation:
        """
        Evaluate one unordered pair (A, B).  Tries both directions and returns the
        best result — either accepted or the furthest-progressed rejection.
        """
        pair_label = f"{plat_a.value}<->{plat_b.value}"
        fee_a = self._fee_bps(plat_a) / 10_000
        fee_b = self._fee_bps(plat_b) / 10_000

        yes_ask_a = v_a.mid + v_a.spread / 2
        no_ask_a  = (1.0 - v_a.mid) + v_a.spread / 2
        yes_ask_b = v_b.mid + v_b.spread / 2
        no_ask_b  = (1.0 - v_b.mid) + v_b.spread / 2

        def _pair_reject(reason: str, **kw) -> ArbEvaluation:
            return ArbEvaluation(
                market_id=fv.market_id, evaluated_at=now, signal_age_ms=signal_age_ms,
                arb_signal=fv.arb_signal, accepted=False, rejection_reason=reason,
                pair=pair_label, **kw,
            )

        # Spread check
        for spread, ask, name in [(v_a.spread, yes_ask_a, plat_a.value), (v_b.spread, yes_ask_b, plat_b.value)]:
            if ask > 0 and spread / ask > self._cfg.max_spread_fraction:
                self.rejected_spread += 1
                return _pair_reject(f"spread_too_wide:{name}={spread / ask:.3f}")

        # Two directions
        gross_dirs: dict = {
            f"{plat_a.value.upper()}_YES_{plat_b.value.upper()}_NO": (yes_ask_a, no_ask_b, fee_a, fee_b),
            f"{plat_b.value.upper()}_YES_{plat_a.value.upper()}_NO": (yes_ask_b, no_ask_a, fee_b, fee_a),
        }

        best: Optional[ArbEvaluation] = None

        for direction, (l1_ask, l2_ask, f1, f2) in gross_dirs.items():
            gross = 1.0 - l1_ask - l2_ask
            net_b4 = gross - f1 - f2
            if net_b4 <= 0:
                if best is None:
                    best = _pair_reject(f"no_edge_dir:{direction}")
                continue

            is_a_first = direction.startswith(plat_a.value.upper())
            l1_plat, l1_side, l1_bid = (plat_a, Side.BUY_YES, v_a.mid - v_a.spread / 2) if is_a_first else (plat_b, Side.BUY_YES, v_b.mid - v_b.spread / 2)
            l2_plat, l2_side, l2_bid = (plat_b, Side.BUY_NO, (1.0 - v_b.mid) - v_b.spread / 2) if is_a_first else (plat_a, Side.BUY_NO, (1.0 - v_a.mid) - v_a.spread / 2)
            l1_depth, l2_depth = (v_a.ask_depth, v_b.ask_depth) if is_a_first else (v_b.ask_depth, v_a.ask_depth)
            l1_ofi, l2_ofi = (v_a.ofi, v_b.ofi) if is_a_first else (v_b.ofi, v_a.ofi)

            max_order_usdc = self._cfg.max_order_usdc
            if fv.days_to_resolution is not None and fv.days_to_resolution < 1.0:
                max_order_usdc *= 0.5
            fillable1 = min(max_order_usdc, l1_depth * self._cfg.fill_certainty)
            fillable2 = min(max_order_usdc, l2_depth * self._cfg.fill_certainty)
            raw_size = min(fillable1, fillable2)

            if raw_size < self._cfg.min_order_usdc:
                self.rejected_depth += 1
                if best is None or not best.accepted:
                    best = _pair_reject(
                        f"fillable=${raw_size:.2f}<min=${self._cfg.min_order_usdc:.2f} (d1={l1_depth:.0f} d2={l2_depth:.0f})",
                        direction=direction, raw_size_usdc=raw_size,
                    )
                continue

            l1_fee_bps = self._fee_bps(l1_plat)
            l2_fee_bps = self._fee_bps(l2_plat)
            c1 = estimate_taker_cost(raw_size, l1_ask, l1_bid, l1_depth, l1_fee_bps, l1_ofi)
            c2 = estimate_taker_cost(raw_size, l2_ask, l2_bid, l2_depth, l2_fee_bps, l2_ofi)
            c1_frac = c1.as_fraction(raw_size)
            c2_frac = c2.as_fraction(raw_size)
            net_edge = gross - c1_frac - c2_frac
            net_edge = self._adjust_for_ofi(net_edge, l1_ofi, l2_ofi)

            min_net_edge = self._dynamic_min_edge(getattr(fv, "vol_regime", None))
            if fv.vol_30s is not None and fv.vol_30s > 0.01:
                min_net_edge *= 1.5
            min_depth = min(v_a.bid_depth, v_a.ask_depth, v_b.bid_depth, v_b.ask_depth)
            if min_depth < 100:
                min_net_edge *= 2.0
            if ctx is not None:
                min_net_edge *= max(0.1, 1.0 + ctx.confidence_adjustment)

            if net_edge < min_net_edge:
                if best is None or (not best.accepted and (best.net_edge is None or net_edge > best.net_edge)):
                    best = _pair_reject(
                        f"net_edge={net_edge:.4f}<min={min_net_edge:.4f} (gross={gross:.4f} c1={c1_frac:.4f} c2={c2_frac:.4f})",
                        direction=direction, leg1_cost_frac=c1_frac, leg2_cost_frac=c2_frac,
                        net_edge=net_edge, raw_size_usdc=raw_size,
                    )
                continue

            edge_buffer = net_edge - min_net_edge
            final_size = raw_size
            if edge_buffer < 0.008:
                final_size = max(self._cfg.min_order_usdc, raw_size * (0.5 + (edge_buffer / 0.008) * 0.5))

            group_id = str(uuid.uuid4())
            expiry_ms = now + self._cfg.arb_expiry_ms
            try:
                leg1 = OrderProposal(
                    proposal_id=str(uuid.uuid4()), market_id=fv.market_id, platform=l1_plat,
                    side=l1_side, size_usdc=round(final_size, 2),
                    limit_price=round(max(0.001, min(0.999, l1_ask)), 4),
                    order_type=OrderType.LIMIT, strategy_id=StrategyId.ARB,
                    leg_group_id=group_id, leg_number=ArbLeg.LEG_1,
                    min_fill_ratio=self._cfg.min_fill_ratio, expiry_ms=expiry_ms, source_ts=fv.ts,
                )
                leg2 = OrderProposal(
                    proposal_id=str(uuid.uuid4()), market_id=fv.market_id, platform=l2_plat,
                    side=l2_side, size_usdc=round(final_size, 2),
                    limit_price=round(max(0.001, min(0.999, l2_ask)), 4),
                    order_type=OrderType.LIMIT, strategy_id=StrategyId.ARB,
                    leg_group_id=group_id, leg_number=ArbLeg.LEG_2,
                    min_fill_ratio=None, expiry_ms=expiry_ms, source_ts=fv.ts,
                )
            except Exception as exc:
                continue

            valid1, err1 = self._validate_proposal(leg1)
            valid2, err2 = self._validate_proposal(leg2)
            if not valid1 or not valid2:
                continue

            if best is None or not best.accepted or net_edge > best.net_edge:
                result = ArbEvaluation(
                    market_id=fv.market_id, evaluated_at=now, signal_age_ms=signal_age_ms,
                    arb_signal=fv.arb_signal, accepted=True, rejection_reason=None,
                    direction=direction, pair=pair_label,
                    leg1_cost_frac=c1_frac, leg2_cost_frac=c2_frac, net_edge=net_edge,
                    raw_size_usdc=raw_size, final_size_usdc=final_size,
                    leg1_proposal=leg1, leg2_proposal=leg2,
                )
                best = result

        return best if best is not None else _pair_reject("no_directional_edge")

    def evaluate(
        self, fv: FeatureVector, now_ts: Optional[int] = None, ctx: Optional[SignalContext] = None
    ) -> ArbEvaluation:
        self.evaluated += 1
        now = now_ts if now_ts is not None else self._clock.now_ms()
        signal_age_ms = now - fv.ts

        def _reject(reason: str) -> ArbEvaluation:
            return ArbEvaluation(
                market_id=fv.market_id, evaluated_at=now, signal_age_ms=signal_age_ms,
                arb_signal=fv.arb_signal, accepted=False, rejection_reason=reason,
            )

        if math.isnan(fv.arb_signal):
            self.rejected_stale += 1
            return _reject(f"stale_data:{[p.value for p in fv.stale_markets]}")

        if signal_age_ms > self._cfg.max_signal_age_ms:
            self.rejected_stale += 1
            return _reject(f"signal_age={signal_age_ms}ms > {self._cfg.max_signal_age_ms}ms")

        if fv.arb_signal <= 0.0:
            self.rejected_no_edge += 1
            return _reject(f"arb_signal={fv.arb_signal:.5f}<=0")

        if fv.days_to_resolution is not None and fv.days_to_resolution < self._cfg.min_days_to_resolution:
            self.rejected_no_edge += 1
            return _reject(f"days_to_resolution={fv.days_to_resolution:.2f}<min={self._cfg.min_days_to_resolution}d")

        if len(fv.venues) < 2:
            return _reject("need_at_least_2_venues")

        platforms = list(fv.venues.keys())
        best: Optional[ArbEvaluation] = None

        for i in range(len(platforms)):
            for j in range(i + 1, len(platforms)):
                result = self._evaluate_pair(
                    fv, now, signal_age_ms, ctx,
                    platforms[i], fv.venues[platforms[i]],
                    platforms[j], fv.venues[platforms[j]],
                )
                if best is None or (result.accepted and not best.accepted):
                    best = result
                elif result.accepted and best.accepted and result.net_edge > best.net_edge:
                    best = result

        if best is not None:
            if best.accepted:
                self.accepted += 1
                logger.info(
                    "ARB ACCEPTED market=%s pair=%s dir=%s size=$%.2f net_edge=%.4f age=%dms",
                    fv.market_id, best.pair, best.direction, best.final_size_usdc, best.net_edge, signal_age_ms,
                )
            return best

        return _reject("no_arbitrage_in_any_pair")

    def _validate_proposal(self, proposal: OrderProposal) -> tuple[bool, Optional[str]]:
        """Validate order proposal before returning it.

        Returns (is_valid, error_message_or_None).
        """
        issues: list[str] = []

        if proposal.size_usdc <= 0:
            issues.append(f"size_usdc={proposal.size_usdc} must be > 0")

        if not (0.001 <= proposal.limit_price <= 0.999):
            issues.append(f"limit_price={proposal.limit_price} out of range [0.001, 0.999]")

        if proposal.strategy_id == StrategyId.ARB:
            if not proposal.leg_group_id:
                issues.append("leg_group_id required for ARB orders")
            if proposal.leg_number is None:
                issues.append("leg_number required for ARB orders")
            if proposal.leg_number == ArbLeg.LEG_1 and proposal.min_fill_ratio is None:
                issues.append("min_fill_ratio required on ARB leg 1")
            if proposal.leg_number == ArbLeg.LEG_2 and proposal.min_fill_ratio is not None:
                issues.append("min_fill_ratio must be None on ARB leg 2")

        return (len(issues) == 0, "; ".join(issues) if issues else None)

    def reload_config(self, config: ArbConfig) -> None:
        self._cfg = config
