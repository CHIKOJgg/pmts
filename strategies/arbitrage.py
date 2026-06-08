"""strategies/arbitrage.py — Cross-venue arbitrage with strict feasibility checks."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Optional

from ai.signal_context import SignalContext
from data.models import FeatureVector
from execution.models import OrderProposal
from src.clock import Clock, LiveClock
from src.enums import ArbLeg, OrderType, Platform, Side, StrategyId

logger = logging.getLogger(__name__)

# Cost model constants
IMPACT_FACTOR: float = 0.012  # sqrt-impact; calibrated to thin books
OFI_ADVERSE_THRESHOLD: float = 0.25  # OFI above this → adversity premium
OFI_ADVERSE_MULT: float = 1.60  # impact multiplier when OFI adverse
MIN_DEPTH_USDC: float = 10.0  # minimum depth for reliable cost estimate
FILL_CERTAINTY: float = 0.65  # fraction of displayed depth actually fillable

# Advanced signals constants
LATENCY_ARB_STALENESS_MS: int = 500  # staleness delta for latency arb detection
LATENCY_ARB_SIZE_BOOST: float = 1.20  # position size boost for latency arb

ARB_EXPIRY_MS: int = 2_000  # 2-second deadline for both legs


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
    min_net_edge: float = 0.006  # 0.6% after all costs
    max_spread_fraction: float = 0.07  # reject if spread > 7% of ask
    fill_certainty: float = FILL_CERTAINTY
    min_fill_ratio: float = 0.80  # abort leg-2 if leg-1 fills < 80%
    max_order_usdc: float = 200.0
    min_order_usdc: float = 5.0
    max_signal_age_ms: int = 300  # reject signals older than 300 ms
    arb_expiry_ms: int = ARB_EXPIRY_MS
    pm_fee_bps: int = 20  # Polymarket taker fee (configurable)
    op_fee_bps: int = 25  # Opinion Markets taker fee (configurable)
    min_days_to_resolution: float = 0.0  # hard reject floor; sizing is reduced below 1 day
    ofi_adverse_threshold: float = OFI_ADVERSE_THRESHOLD  # NEW
    ofi_adverse_mult: float = OFI_ADVERSE_MULT  # NEW
    latency_arb_staleness_ms: int = LATENCY_ARB_STALENESS_MS  # NEW
    latency_arb_size_boost: float = LATENCY_ARB_SIZE_BOOST  # NEW


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
    Stateless evaluator. Call evaluate(fv) every tick.

    Returns ArbEvaluation. If accepted=True, leg1_proposal and leg2_proposal
    are ready to send to CRG. If accepted=False, rejection_reason explains why.
    """

    def __init__(
        self,
        config: ArbConfig = ArbConfig(),
        clock: Clock = LiveClock(),
    ) -> None:
        self._cfg = config
        # Fees are read from config; defaults provided for backward compatibility
        self._pm_fee = getattr(config, "pm_fee_bps", 20)
        self._op_fee = getattr(config, "op_fee_bps", 25)
        self._clock: Clock = clock

        self.evaluated: int = 0
        self.accepted: int = 0
        self.rejected_stale: int = 0
        self.rejected_no_edge: int = 0
        self.rejected_spread: int = 0
        self.rejected_depth: int = 0
        self.rejected_ofi: int = 0

    # ── Advanced signal adjustments ──────────────────────────────────────────────

    def _adjust_for_ofi(self, raw_edge: float, ofi_pm: float, ofi_op: float) -> float:
        """Apply OFI adverse-selection penalty to net edge."""
        ofi_net = ofi_pm - ofi_op  # positive = flow toward PM, favorable for arb
        if abs(ofi_net) > self._cfg.ofi_adverse_threshold:
            penalty = 1 + (abs(ofi_net) - self._cfg.ofi_adverse_threshold) * self._cfg.ofi_adverse_mult
            return raw_edge / penalty
        return raw_edge

    def _dynamic_min_edge(self, vol_regime: Optional[str]) -> float:
        """Tighten edge requirement in low-vol (thin books), relax in high-vol."""
        if vol_regime is None:
            return self._cfg.min_net_edge
        return {
            "LOW":    self._cfg.min_net_edge * 1.5,   # tighter in calm markets
            "NORMAL": self._cfg.min_net_edge,
            "HIGH":   self._cfg.min_net_edge * 0.8,   # relax in volatile markets
            "SPIKE":  self._cfg.min_net_edge * 0.7,   # even more relaxed in spikes
        }.get(vol_regime, self._cfg.min_net_edge)

    def _detect_latency_arb(self, fv: FeatureVector) -> bool:
        """Detect cross-venue latency arbitrage opportunity."""
        # Check if feed age delta between venues exceeds threshold
        # This requires feed_age_ms to be available on FeatureVector
        # For now, we check if spread delta suggests latency arb
        spread_delta = abs(fv.spread_pm - fv.spread_op)
        return spread_delta > self._cfg.latency_arb_staleness_ms / 10000.0

    # ── End advanced signal adjustments ────────────────────────────────────────

    def evaluate(
        self, fv: FeatureVector, now_ts: Optional[int] = None, ctx: Optional[SignalContext] = None
    ) -> ArbEvaluation:
        """
        Evaluate arb signal for one FeatureVector.

        now_ts: simulated current time in ms (for backtest).  If None,
                wall-clock time is used (live trading).
        ctx: AI SignalContext to modulate thresholds.
        """
        self.evaluated += 1
        now = now_ts if now_ts is not None else self._clock.now_ms()
        signal_age_ms = now - fv.ts

        def _reject(reason: str) -> ArbEvaluation:
            return ArbEvaluation(
                market_id=fv.market_id,
                evaluated_at=now,
                signal_age_ms=signal_age_ms,
                arb_signal=fv.arb_signal,
                accepted=False,
                rejection_reason=reason,
            )

        # ── Guard 1: NaN signal ───────────────────────────────────────────────
        if math.isnan(fv.arb_signal):
            self.rejected_stale += 1
            return _reject(f"stale_data:{[p.value for p in fv.stale_markets]}")

        # ── Guard 2: Signal age ───────────────────────────────────────────────
        if signal_age_ms > self._cfg.max_signal_age_ms:
            self.rejected_stale += 1
            return _reject(f"signal_age={signal_age_ms}ms > {self._cfg.max_signal_age_ms}ms")

        # ── Guard 3: Pre-cost edge ────────────────────────────────────────────
        if fv.arb_signal <= 0.0:
            self.rejected_no_edge += 1
            return _reject(f"arb_signal={fv.arb_signal:.5f}<=0")

        # ── Guard 4: Spread feasibility ───────────────────────────────────────
        yes_ask_pm = fv.mid_pm + fv.spread_pm / 2
        no_ask_pm = (1.0 - fv.mid_pm) + fv.spread_pm / 2
        yes_ask_op = fv.mid_op + fv.spread_op / 2
        no_ask_op = (1.0 - fv.mid_op) + fv.spread_op / 2

        for spread, ask, name in [
            (fv.spread_pm, yes_ask_pm, "PM"),
            (fv.spread_op, yes_ask_op, "OP"),
        ]:
            if ask > 0 and spread / ask > self._cfg.max_spread_fraction:
                self.rejected_spread += 1
                return _reject(f"spread_too_wide:{name}={spread / ask:.3f}")

        # ── Direction selection (both directions computed from actual ask prices)
        fee_pm = self._pm_fee / 10_000
        fee_op = self._op_fee / 10_000

        gross_a = 1.0 - yes_ask_pm - no_ask_op  # buy YES on PM, NO on OP
        gross_b = 1.0 - yes_ask_op - no_ask_pm  # buy YES on OP, NO on PM
        net_a = gross_a - fee_pm - fee_op
        net_b = gross_b - fee_pm - fee_op

        if net_a <= 0 and net_b <= 0:
            self.rejected_no_edge += 1
            return _reject(f"no_directional_edge:A={net_a:.4f} B={net_b:.4f}")

        # Choose better direction; tie-break by depth
        if net_a >= net_b:
            direction = "PM_YES_OP_NO"
            l1_plat, l1_side, l1_ask, l1_bid = (
                Platform.POLYMARKET,
                Side.BUY_YES,
                yes_ask_pm,
                fv.mid_pm - fv.spread_pm / 2,
            )
            l2_plat, l2_side, l2_ask, l2_bid = (
                Platform.OPINION,
                Side.BUY_NO,
                no_ask_op,
                (1.0 - fv.mid_op) - fv.spread_op / 2,
            )
            l1_depth, l2_depth = fv.ask_depth_pm, fv.ask_depth_op
            l1_ofi, l2_ofi = fv.ofi_pm, fv.ofi_op
            gross_edge = gross_a
        else:
            direction = "OP_YES_PM_NO"
            l1_plat, l1_side, l1_ask, l1_bid = (
                Platform.OPINION,
                Side.BUY_YES,
                yes_ask_op,
                fv.mid_op - fv.spread_op / 2,
            )
            l2_plat, l2_side, l2_ask, l2_bid = (
                Platform.POLYMARKET,
                Side.BUY_NO,
                no_ask_pm,
                (1.0 - fv.mid_pm) - fv.spread_pm / 2,
            )
            l1_depth, l2_depth = fv.ask_depth_op, fv.ask_depth_pm
            l1_ofi, l2_ofi = fv.ofi_op, fv.ofi_pm
            gross_edge = gross_b

        # ── Guard 4.5: Near-expiry market check ───────────────────────────────
        min_days = getattr(self._cfg, "min_days_to_resolution", 1.0)
        if fv.days_to_resolution is not None and fv.days_to_resolution < min_days:
            self.rejected_no_edge += 1
            return _reject(f"days_to_resolution={fv.days_to_resolution:.2f}<min={min_days}d")

        # ── Guard 5: Depth / fillable size ────────────────────────────────────
        max_order_usdc = self._cfg.max_order_usdc
        if fv.days_to_resolution is not None and fv.days_to_resolution < 1.0:
            max_order_usdc *= 0.5

        fillable1 = min(max_order_usdc, l1_depth * self._cfg.fill_certainty)
        fillable2 = min(max_order_usdc, l2_depth * self._cfg.fill_certainty)
        raw_size = min(fillable1, fillable2)

        if raw_size < self._cfg.min_order_usdc:
            self.rejected_depth += 1
            return ArbEvaluation(
                market_id=fv.market_id,
                evaluated_at=now,
                signal_age_ms=signal_age_ms,
                arb_signal=fv.arb_signal,
                accepted=False,
                direction=direction,
                raw_size_usdc=raw_size,
                rejection_reason=(
                    f"fillable=${raw_size:.2f}<min=${self._cfg.min_order_usdc:.2f} "
                    f"(d1={l1_depth:.0f} d2={l2_depth:.0f})"
                ),
            )

        # ── Guard 6: Net edge after slippage ──────────────────────────────────
        l1_fee_bps = self._pm_fee if l1_plat == Platform.POLYMARKET else self._op_fee
        l2_fee_bps = self._op_fee if l2_plat == Platform.OPINION else self._pm_fee

        c1 = estimate_taker_cost(raw_size, l1_ask, l1_bid, l1_depth, l1_fee_bps, l1_ofi)
        c2 = estimate_taker_cost(raw_size, l2_ask, l2_bid, l2_depth, l2_fee_bps, l2_ofi)
        c1_frac = c1.as_fraction(raw_size)
        c2_frac = c2.as_fraction(raw_size)
        net_edge = gross_edge - c1_frac - c2_frac

        # ── Advanced signals: OFI adjustment ──────────────────────────────────
        net_edge = self._adjust_for_ofi(net_edge, l1_ofi, l2_ofi)

        # ── Advanced signals: Dynamic min_net_edge based on vol regime ─────────
        min_net_edge = self._dynamic_min_edge(fv.vol_regime)

        if fv.vol_30s is not None and fv.vol_30s > 0.01:
            min_net_edge *= 1.5

        min_depth = min(fv.bid_depth_pm, fv.ask_depth_pm, fv.bid_depth_op, fv.ask_depth_op)
        if min_depth < 100:
            min_net_edge *= 2.0

        if ctx is not None:
            min_net_edge *= max(0.1, 1.0 + ctx.confidence_adjustment)

        # ── Advanced signals: Latency arb detection ────────────────────────────
        latency_arb_boost = 1.0
        if self._detect_latency_arb(fv):
            latency_arb_boost = self._cfg.latency_arb_size_boost
            logger.info("LATENCY ARB DETECTED market=%s boost=%.2f", fv.market_id, latency_arb_boost)

        if net_edge < min_net_edge:
            self.rejected_no_edge += 1
            return ArbEvaluation(
                market_id=fv.market_id,
                evaluated_at=now,
                signal_age_ms=signal_age_ms,
                arb_signal=fv.arb_signal,
                accepted=False,
                direction=direction,
                leg1_cost_frac=c1_frac,
                leg2_cost_frac=c2_frac,
                net_edge=net_edge,
                raw_size_usdc=raw_size,
                rejection_reason=(
                    f"net_edge={net_edge:.4f}<min={min_net_edge:.4f} "
                    f"(gross={gross_edge:.4f} c1={c1_frac:.4f} c2={c2_frac:.4f})"
                ),
            )

        # ── Scale down when edge is borderline ────────────────────────────────
        edge_buffer = net_edge - min_net_edge
        if edge_buffer < 0.008:
            scale = 0.5 + (edge_buffer / 0.008) * 0.5
            final_size = max(self._cfg.min_order_usdc, raw_size * scale)
        else:
            final_size = raw_size

        # Apply latency arb size boost if detected
        final_size *= latency_arb_boost

        # ── Build proposals ───────────────────────────────────────────────────
        group_id = str(uuid.uuid4())
        expiry_ms = now + self._cfg.arb_expiry_ms

        try:
            leg1 = OrderProposal(
                proposal_id=str(uuid.uuid4()),
                market_id=fv.market_id,
                platform=l1_plat,
                side=l1_side,
                size_usdc=round(final_size, 2),
                limit_price=round(max(0.001, min(0.999, l1_ask)), 4),
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.ARB,
                leg_group_id=group_id,
                leg_number=ArbLeg.LEG_1,
                min_fill_ratio=self._cfg.min_fill_ratio,
                expiry_ms=expiry_ms,
                source_ts=fv.ts,
            )
            leg2 = OrderProposal(
                proposal_id=str(uuid.uuid4()),
                market_id=fv.market_id,
                platform=l2_plat,
                side=l2_side,
                size_usdc=round(final_size, 2),
                limit_price=round(max(0.001, min(0.999, l2_ask)), 4),
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.ARB,
                leg_group_id=group_id,
                leg_number=ArbLeg.LEG_2,
                min_fill_ratio=None,
                expiry_ms=expiry_ms,
                source_ts=fv.ts,
            )
        except Exception as exc:
            return _reject(f"proposal_build_failed:{exc}")

        # ── Validate proposals before returning ───────────────────────────────
        valid1, err1 = self._validate_proposal(leg1)
        valid2, err2 = self._validate_proposal(leg2)
        if not valid1 or not valid2:
            return _reject(f"proposal_validation_failed: leg1={err1}, leg2={err2}")

        self.accepted += 1
        logger.info(
            "ARB ACCEPTED market=%s dir=%s size=$%.2f net_edge=%.4f c1=%.4f c2=%.4f age=%dms",
            fv.market_id,
            direction,
            final_size,
            net_edge,
            c1_frac,
            c2_frac,
            signal_age_ms,
        )
        return ArbEvaluation(
            market_id=fv.market_id,
            evaluated_at=now,
            signal_age_ms=signal_age_ms,
            arb_signal=fv.arb_signal,
            accepted=True,
            rejection_reason=None,
            direction=direction,
            leg1_cost_frac=c1_frac,
            leg2_cost_frac=c2_frac,
            net_edge=net_edge,
            raw_size_usdc=raw_size,
            final_size_usdc=final_size,
            leg1_proposal=leg1,
            leg2_proposal=leg2,
        )

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
