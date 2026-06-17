"""strategies/delta_neutral.py — Stoikov MM quotes and delta-neutral hedging."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import List, Optional

from ai.signal_context import SignalContext
from data.models import FeatureVector, VenueSnapshot
from execution.models import OrderProposal
from portfolio.manager import FillRecord
from src.clock import Clock, LiveClock
from src.enums import OrderType, Platform, Side, StrategyId

logger = logging.getLogger(__name__)

MM_EXPIRY_MS: int = 30_000  # 30-second MM order lifetime


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeltaNeutralConfig:
    hedge_threshold: float = 10.0  # |delta| above which to hedge
    residual_band: float = 2.0  # target |delta| after hedge (avoids oscillation)
    max_hedge_usdc: float = 150.0
    min_hedge_usdc: float = 5.0
    venue_tolerance: float = 0.005  # price within this → use depth as tie-break
    min_days_to_resolution: float = 3.0  # suppress MM within this many days
    gamma: float = 0.10  # Stoikov risk aversion
    k: float = 1.50  # Stoikov order arrival rate
    mm_quote_size_usdc: float = 25.0
    mm_expiry_ms: int = MM_EXPIRY_MS

    def __post_init__(self) -> None:
        if self.residual_band >= self.hedge_threshold:
            raise ValueError("residual_band must be < hedge_threshold")


DEFAULT_DN_CONFIG = DeltaNeutralConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HedgeDecision:
    market_id: str
    current_delta: float
    target_delta: float
    hedge_tokens: float
    hedge_direction: Optional[str]
    venue: Optional[Platform]
    proposal: Optional[OrderProposal]
    should_hedge: bool
    reason: str


@dataclass(frozen=True)
class MMQuotes:
    market_id: str
    platform: Platform
    bid_proposal: Optional[OrderProposal]
    ask_proposal: Optional[OrderProposal]
    reservation_mid: Optional[float]
    spread: Optional[float]
    suppressed: bool
    suppression_reason: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# DeltaNeutralStrategy
# ─────────────────────────────────────────────────────────────────────────────


class DeltaNeutralStrategy:
    """Stateless per-tick evaluator for MM quotes and delta hedging."""

    def __init__(
        self,
        config: DeltaNeutralConfig = DEFAULT_DN_CONFIG,
        clock: Clock = LiveClock(),
    ) -> None:
        self._cfg = config
        self._clock = clock
        self.hedges_proposed: int = 0
        self.hedges_skipped: int = 0
        self.mm_quotes_issued: int = 0
        self.mm_quotes_suppressed: int = 0

    # ── Hedge ─────────────────────────────────────────────────────────────────

    def evaluate_hedge(self, fv: FeatureVector, ctx: Optional[SignalContext] = None) -> HedgeDecision:
        """
        Decide whether and how to reduce the current delta.

        Uses residual band to prevent oscillation: target = ±residual_band,
        not zero. Cross-venue correlation check: if both venues have nearly
        identical prices, warns that the hedge may not reduce aggregate risk.
        """
        cfg = self._cfg
        delta = fv.portfolio_delta

        hedge_threshold = cfg.hedge_threshold
        if ctx and ctx.hedge_urgency > 0:
            hedge_threshold = cfg.hedge_threshold * max(0.2, 1.0 - ctx.hedge_urgency)

        def _skip(reason: str) -> HedgeDecision:
            self.hedges_skipped += 1
            return HedgeDecision(
                market_id=fv.market_id,
                current_delta=delta,
                target_delta=delta,
                hedge_tokens=0.0,
                hedge_direction=None,
                venue=None,
                proposal=None,
                should_hedge=False,
                reason=reason,
            )

        if abs(delta) <= hedge_threshold:
            return _skip(f"|delta|={abs(delta):.2f}<=threshold={hedge_threshold:.2f}")

        # Cross-venue correlation warning
        pm_v = fv.venues.get(Platform.POLYMARKET)
        op_v = fv.venues.get(Platform.OPINION)
        if pm_v is not None and op_v is not None and abs(pm_v.mid - op_v.mid) < 0.01:
            logger.warning(
                "Cross-venue prices nearly identical for %s (pm=%.4f op=%.4f) — "
                "hedge may not reduce aggregate risk if venues are correlated",
                fv.market_id,
                pm_v.mid,
                op_v.mid,
            )

        if delta > 0:
            target, hedge_tokens, direction = (cfg.residual_band, delta - cfg.residual_band, "buy_no")
        else:
            target, hedge_tokens, direction = (-cfg.residual_band, abs(delta) - cfg.residual_band, "buy_yes")

        # Both venues stale → can't assess execution quality
        if len(fv.stale_markets) == 2:
            return HedgeDecision(
                market_id=fv.market_id,
                current_delta=delta,
                target_delta=target,
                hedge_tokens=hedge_tokens,
                hedge_direction=direction,
                venue=None,
                proposal=None,
                should_hedge=False,
                reason="both_venues_stale",
            )

        venue, hedge_price = self._select_hedge_venue(fv, direction)

        hedge_usdc = min(hedge_tokens * hedge_price, cfg.max_hedge_usdc)
        if hedge_usdc < cfg.min_hedge_usdc:
            return HedgeDecision(
                market_id=fv.market_id,
                current_delta=delta,
                target_delta=target,
                hedge_tokens=hedge_tokens,
                hedge_direction=direction,
                venue=venue,
                proposal=None,
                should_hedge=False,
                reason=f"hedge_too_small:${hedge_usdc:.2f}<${cfg.min_hedge_usdc:.2f}",
            )

        side = Side.BUY_NO if direction == "buy_no" else Side.BUY_YES
        now = self._clock.now_ms()
        try:
            proposal = OrderProposal(
                proposal_id=str(uuid.uuid4()),
                market_id=fv.market_id,
                platform=venue,
                side=side,
                size_usdc=round(hedge_usdc, 2),
                limit_price=round(max(0.001, min(0.999, hedge_price)), 4),
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.HEDGE,
                expiry_ms=now + cfg.mm_expiry_ms,
                source_ts=fv.ts,
            )
        except Exception as exc:
            return HedgeDecision(
                market_id=fv.market_id,
                current_delta=delta,
                target_delta=target,
                hedge_tokens=hedge_tokens,
                hedge_direction=direction,
                venue=venue,
                proposal=None,
                should_hedge=False,
                reason=f"proposal_error:{exc}",
            )

        self.hedges_proposed += 1
        logger.info(
            "HEDGE market=%s Δ=%.2f→%.2f %s $%.2f@%.4f on %s",
            fv.market_id,
            delta,
            target,
            direction,
            hedge_usdc,
            hedge_price,
            venue.value,
        )
        return HedgeDecision(
            market_id=fv.market_id,
            current_delta=delta,
            target_delta=target,
            hedge_tokens=hedge_tokens,
            hedge_direction=direction,
            venue=venue,
            proposal=proposal,
            should_hedge=True,
            reason=f"|delta|={abs(delta):.2f}>threshold={hedge_threshold:.2f}",
        )

    # ── Market Making ─────────────────────────────────────────────────────────

    def evaluate_mm(
        self,
        fv: FeatureVector,
        platform: Platform,
        ctx: Optional[SignalContext] = None,
        fills: Optional[List[FillRecord]] = None,
    ) -> Optional[MMQuotes]:
        """
        Compute Stoikov reservation-price MM quotes for one venue.

        The reservation price skews against inventory:
          r = mid − delta × gamma × sigma² × (T − t)
        This makes our ask more competitive when long YES (sell faster),
        and our bid more competitive when long NO (buy YES faster).

        If recent fills indicate adverse selection, the spread is doubled.
        """
        cfg = self._cfg

        def _suppress(reason: str) -> MMQuotes:
            self.mm_quotes_suppressed += 1
            return MMQuotes(
                market_id=fv.market_id,
                platform=platform,
                bid_proposal=None,
                ask_proposal=None,
                reservation_mid=None,
                spread=None,
                suppressed=True,
                suppression_reason=reason,
            )

        if ctx and ctx.suppress_mm:
            return _suppress("ai_suppression")

        if fv.vol_30s is None:
            return _suppress("vol_30s_not_ready")

        days = fv.days_to_resolution
        if days is not None and days < 1.0:
            return _suppress("near_expiry:<1d")

        if days is not None and days <= cfg.min_days_to_resolution:
            return _suppress(f"near_resolution:{days:.1f}d<={cfg.min_days_to_resolution}d")

        counterpart = Platform.OPINION if platform == Platform.POLYMARKET else Platform.POLYMARKET
        if platform in fv.stale_markets or counterpart in fv.stale_markets:
            return None

        mid = fv.venues[platform].mid
        if mid < 0.05 or mid > 0.95:
            return _suppress(f"near_boundary:mid={mid:.3f}")

        # Book depth checks — don't quote when we'd be the entire book
        venue = fv.venues[platform]
        total_book_depth = venue.bid_depth + venue.ask_depth
        if total_book_depth < 50.0:
            return _suppress(f"book_too_thin:depth=${total_book_depth:.0f}")
        if venue.bid_depth < 10.0 or venue.ask_depth < 10.0:
            return _suppress(f"one_sided_book:bid=${venue.bid_depth:.0f},ask=${venue.ask_depth:.0f}")

        # Stoikov parameters
        sigma = fv.vol_30s                      # 30-second rolling stdev
        gamma = cfg.gamma
        k = cfg.k
        T_minus_t = max(0.01, days if days is not None else 1.0)
        delta = fv.portfolio_delta

        # Convert T from days to 30-second units so sigma² × T is dimensionally consistent
        t_30s_units = T_minus_t * 2880.0  # 24h × 60m × 60s / 30s = 2880

        # Reservation price
        r_mid = mid - delta * gamma * (sigma**2) * t_30s_units
        r_mid = max(0.01, min(0.99, r_mid))

        # Optimal spread
        base_spread = gamma * (sigma**2) * t_30s_units
        arrival_adj = (2.0 / gamma) * math.log(1.0 + gamma / k)
        half_spread = max(0.005, min(0.05, (base_spread + arrival_adj) / 2.0))

        position_value = abs(delta) * mid  # convert token delta → USDC for unit consistency
        size = self._compute_adaptive_quote_size(position_value, cfg.max_hedge_usdc)
        now = self._clock.now_ms()

        # Adverse selection detection — widen spread if recent fills are adverse
        if fills and self._detect_adverse_selection(fv.market_id, fills):
            half_spread = self._widen_spread_for_adverse_selection(half_spread)
            logger.info("Adverse selection detected for %s — spread widened to %.4f", fv.market_id, half_spread)

        # Isolation penalty — widen spread when our quote is a large fraction of book
        min_depth = min(venue.bid_depth, venue.ask_depth)
        if min_depth > 0:
            quote_fraction = size / min_depth
            if quote_fraction > 0.5:
                isolation_mult = 1.0 + (quote_fraction - 0.5) * 2.0
                half_spread *= isolation_mult
                logger.debug(
                    "Isolation penalty for %s: quote=%.0f%% of min_depth, spread widened to %.4f",
                    fv.market_id, quote_fraction * 100, half_spread,
                )

        bid_price = max(0.01, r_mid - half_spread)
        ask_price = min(0.99, r_mid + half_spread)
        if ask_price - bid_price < 0.002:
            ask_price = bid_price + 0.002

        try:
            bid_p = OrderProposal(
                proposal_id=str(uuid.uuid4()),
                market_id=fv.market_id,
                platform=platform,
                side=Side.BUY_YES,
                size_usdc=size,
                limit_price=round(bid_price, 4),
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.MM,
                expiry_ms=now + cfg.mm_expiry_ms,
                source_ts=fv.ts,
            )
            ask_p = OrderProposal(
                proposal_id=str(uuid.uuid4()),
                market_id=fv.market_id,
                platform=platform,
                side=Side.SELL_YES,
                size_usdc=size,
                limit_price=round(ask_price, 4),
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.MM,
                expiry_ms=now + cfg.mm_expiry_ms,
                source_ts=fv.ts,
            )
        except Exception as exc:
            return _suppress(f"proposal_error:{exc}")

        self.mm_quotes_issued += 2
        return MMQuotes(
            market_id=fv.market_id,
            platform=platform,
            bid_proposal=bid_p,
            ask_proposal=ask_p,
            reservation_mid=round(r_mid, 4),
            spread=round(base_spread + arrival_adj, 4),
            suppressed=False,
            suppression_reason=None,
        )

    def reload_config(self, config: DeltaNeutralConfig) -> None:
        self._cfg = config

    def _compute_adaptive_quote_size(self, inventory: float, max_inventory: float) -> float:
        inventory_ratio = abs(inventory) / max_inventory if max_inventory > 0 else 0.0
        if inventory_ratio > 0.8:
            return self._cfg.mm_quote_size_usdc * 0.25
        elif inventory_ratio > 0.6:
            return self._cfg.mm_quote_size_usdc * 0.5
        elif inventory_ratio > 0.4:
            return self._cfg.mm_quote_size_usdc * 0.75
        return self._cfg.mm_quote_size_usdc

    def _detect_adverse_selection(self, market_id: str, fills: List[FillRecord]) -> bool:
        if len(fills) < 5:
            return False
        recent = fills[-5:]
        same_side = all(f.side == recent[0].side for f in recent)
        if same_side:
            price_move = recent[-1].fill_price - recent[0].fill_price
            if recent[0].side == Side.BUY_YES and price_move < -0.02:
                return True
            if recent[0].side == Side.SELL_YES and price_move > 0.02:
                return True
        return False

    def _widen_spread_for_adverse_selection(self, base_spread: float) -> float:
        return base_spread * 2.0

    # ── Venue selection ───────────────────────────────────────────────────────

    def _select_hedge_venue(self, fv: FeatureVector, direction: str) -> tuple[Platform, float]:
        tol = self._cfg.venue_tolerance
        pm_v = fv.venues.get(Platform.POLYMARKET, VenueSnapshot(0, 0, 0, 0, 0))
        op_v = fv.venues.get(Platform.OPINION, VenueSnapshot(0, 0, 0, 0, 0))

        # Calculate cross-venue correlation for better hedging decisions
        price_diff = abs(pm_v.mid - op_v.mid)
        correlation = 1.0 - min(price_diff / 0.1, 1.0)  # 0 diff → corr=1, >10% diff → corr=0

        if direction == "buy_no":
            # Buying NO: want lowest NO ask = highest (1 - YES_bid)
            no_ask_pm = (1.0 - pm_v.mid) + pm_v.spread / 2
            no_ask_op = (1.0 - op_v.mid) + op_v.spread / 2
            depth_pm = pm_v.bid_depth
            depth_op = op_v.bid_depth

            # Prefer venue with better effective price considering correlation
            if correlation > 0.8:
                # High correlation - prefer deeper venue to reduce slippage
                return (Platform.POLYMARKET, no_ask_pm) if depth_pm >= depth_op else (Platform.OPINION, no_ask_op)

            # Low correlation - pick cheaper venue
            if abs(no_ask_pm - no_ask_op) <= tol:
                return (Platform.POLYMARKET, no_ask_pm) if depth_pm >= depth_op else (Platform.OPINION, no_ask_op)
            return (Platform.POLYMARKET, no_ask_pm) if no_ask_pm < no_ask_op else (Platform.OPINION, no_ask_op)
        else:  # buy_yes
            yes_ask_pm = pm_v.mid + pm_v.spread / 2
            yes_ask_op = op_v.mid + op_v.spread / 2
            depth_pm = pm_v.ask_depth
            depth_op = op_v.ask_depth

            if correlation > 0.8:
                return (Platform.POLYMARKET, yes_ask_pm) if depth_pm >= depth_op else (Platform.OPINION, yes_ask_op)

            if abs(yes_ask_pm - yes_ask_op) <= tol:
                return (Platform.POLYMARKET, yes_ask_pm) if depth_pm >= depth_op else (Platform.OPINION, yes_ask_op)
            return (Platform.POLYMARKET, yes_ask_pm) if yes_ask_pm < yes_ask_op else (Platform.OPINION, yes_ask_op)
