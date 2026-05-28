"""
tests/test_core.py — Core test suite. Zero external dependencies.

All tests run with Python stdlib only.
Run with: python -m pytest tests/ -v
      or: python -m unittest discover tests
"""

from __future__ import annotations

import asyncio
import math

# ── Ensure project root is on path ───────────────────────────────────────────
import os
import sys
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.heuristic import heuristic_enhance
from ai.signal_context import CONFIDENCE_MIN, MarketRegime, SignalContext, VolRegime
from backtest.engine import (
    BacktestEngine,
    FillSimulator,
    LatencyModel,
    _max_drawdown,
    _sharpe,
    build_synthetic_tick_stream,
)
from data.models import FeatureVector, MarketSnapshot
from execution.models import ExecutionResult, OrderProposal, OrderSubmission
from execution.order_tracker import OrderTracker, TrackerStatus
from portfolio.manager import FillRecord, PortfolioManager
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import DEFAULT_LIMITS, RiskLimits
from src.errors import CrossedBookError, NegativeHoldings
from src.types import (
    OrderStatus,
    OrderType,
    Platform,
    RejectReason,
    Side,
    StrategyId,
)
from strategies.arbitrage import ArbConfig, ArbitrageStrategy, estimate_taker_cost

# ── Helpers ───────────────────────────────────────────────────────────────────


def uid() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    return int(time.time() * 1000)


def make_portfolio(cash: float = 10_000.0) -> PortfolioManager:
    def price_source(m, p):
        return (0.50, 0.50)

    return PortfolioManager(initial_cash_usdc=cash, price_source=price_source)


def make_risk(portfolio=None, limits=None, kill_token="test-token-secure-123"):
    pm = portfolio or make_portfolio()
    ks = KillSwitch(kill_token)
    lim = limits or DEFAULT_LIMITS
    return RiskEngine(portfolio=pm, kill_switch=ks, limits=lim), ks, pm


def make_proposal(
    size=50.0,
    price=0.50,
    side=Side.BUY_YES,
    strategy=StrategyId.MM,
    market="BTC-Q4",
    platform=Platform.POLYMARKET,
) -> OrderProposal:
    return OrderProposal(
        proposal_id=uid(),
        market_id=market,
        platform=platform,
        side=side,
        size_usdc=size,
        limit_price=price,
        order_type=OrderType.LIMIT,
        strategy_id=strategy,
        expiry_ms=now_ms() + 30_000,
        source_ts=now_ms(),
    )


def make_submission(
    size=50.0,
    price=0.50,
    side=Side.BUY_YES,
    strategy=StrategyId.MM,
) -> OrderSubmission:
    return OrderSubmission(
        order_id=uid(),
        proposal_id=uid(),
        market_id="BTC-Q4",
        platform=Platform.POLYMARKET,
        side=side,
        size_usdc=size,
        limit_price=price,
        order_type=OrderType.LIMIT,
        strategy_id=strategy,
        expiry_ms=now_ms() + 30_000,
        token_quantity=round(size / price, 6),
        submitted_at=now_ms(),
    )


def make_snapshot(
    market="BTC-Q4",
    platform=Platform.POLYMARKET,
    yes_bid=0.44,
    yes_ask=0.46,
    bid_depth=1000.0,
    ask_depth=900.0,
    fee_bps=20,
    is_stale=False,
) -> MarketSnapshot:
    t = now_ms()
    no_mid = 1.0 - (yes_bid + yes_ask) / 2
    return MarketSnapshot(
        market_id=market,
        platform=platform,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=max(0.01, no_mid - 0.01),
        no_ask=min(0.99, no_mid + 0.01),
        bid_depth_usdc=bid_depth,
        ask_depth_usdc=ask_depth,
        taker_fee_bps=fee_bps,
        ts=t,
        received_ts=t,
        is_stale=is_stale,
    )


def make_fv(
    market="BTC-Q4",
    mid_pm=0.46,
    mid_op=0.54,
    spread_pm=0.02,
    spread_op=0.02,
    ofi_pm=0.0,
    ofi_op=0.0,
    vol_30s=0.01,
    days=10.0,
    delta=0.0,
    bid_pm=1000.0,
    ask_pm=900.0,
    bid_op=800.0,
    ask_op=700.0,
    stale=None,
) -> FeatureVector:
    stale = stale or []
    t = now_ms()
    if stale:
        arb = float("nan")
    else:
        arb = 1.0 - (mid_pm + spread_pm / 2) - ((1 - mid_op) + spread_op / 2) - 0.002 - 0.0025
    return FeatureVector(
        market_id=market,
        ts=t,
        computed_ts=t,
        arb_signal=arb,
        stale_markets=stale,
        mid_pm=mid_pm,
        mid_op=mid_op,
        spread_pm=spread_pm,
        spread_op=spread_op,
        ofi_pm=ofi_pm,
        ofi_op=ofi_op,
        vol_30s=vol_30s,
        days_to_resolution=days,
        portfolio_delta=delta,
        bid_depth_pm=bid_pm,
        ask_depth_pm=ask_pm,
        bid_depth_op=bid_op,
        ask_depth_op=ask_op,
    )


def run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ═════════════════════════════════════════════════════════════════════════════
# 1. TYPES AND MODELS
# ═════════════════════════════════════════════════════════════════════════════


class TestTypes(unittest.TestCase):
    def test_side_is_buy(self):
        self.assertTrue(Side.BUY_YES.is_buy)
        self.assertTrue(Side.BUY_NO.is_buy)
        self.assertFalse(Side.SELL_YES.is_buy)
        self.assertFalse(Side.SELL_NO.is_buy)

    def test_side_is_yes(self):
        self.assertTrue(Side.BUY_YES.is_yes)
        self.assertTrue(Side.SELL_YES.is_yes)
        self.assertFalse(Side.BUY_NO.is_yes)
        self.assertFalse(Side.SELL_NO.is_yes)

    def test_order_status_terminal(self):
        self.assertTrue(OrderStatus.FILLED.is_terminal)
        self.assertTrue(OrderStatus.CANCELLED.is_terminal)
        self.assertFalse(OrderStatus.SUBMITTED.is_terminal)
        self.assertFalse(OrderStatus.PARTIAL.is_terminal)

    def test_market_snapshot_validation(self):
        with self.assertRaises(CrossedBookError):
            MarketSnapshot(
                market_id="X",
                platform=Platform.POLYMARKET,
                yes_bid=0.50,
                yes_ask=0.40,  # bid > ask → crossed
                no_bid=0.40,
                no_ask=0.60,
                bid_depth_usdc=100,
                ask_depth_usdc=100,
                taker_fee_bps=20,
                ts=now_ms(),
                received_ts=now_ms(),
            )

    def test_market_snapshot_properties(self):
        s = make_snapshot(yes_bid=0.44, yes_ask=0.46)
        self.assertAlmostEqual(s.yes_mid, 0.45)
        self.assertAlmostEqual(s.yes_spread, 0.02)
        self.assertAlmostEqual(s.taker_fee, 0.002)

    def test_feature_vector_nan_requires_stale(self):
        with self.assertRaises(ValueError):
            FeatureVector(
                market_id="X",
                ts=now_ms(),
                computed_ts=now_ms(),
                arb_signal=float("nan"),
                stale_markets=[],  # NaN but no stale
                mid_pm=0.5,
                mid_op=0.5,
                spread_pm=0.0,
                spread_op=0.0,
                ofi_pm=0.0,
                ofi_op=0.0,
                vol_30s=0.01,
                days_to_resolution=10.0,
                portfolio_delta=0.0,
                bid_depth_pm=0.0,
                ask_depth_pm=0.0,
                bid_depth_op=0.0,
                ask_depth_op=0.0,
            )

    def test_feature_vector_valid_with_stale(self):
        fv = make_fv(stale=[Platform.POLYMARKET])
        self.assertTrue(math.isnan(fv.arb_signal))
        self.assertFalse(fv.arb_tradeable)

    def test_order_proposal_validation(self):
        with self.assertRaises(ValueError):
            make_proposal(size=-1.0)  # negative size

    def test_order_proposal_arb_requires_group(self):
        with self.assertRaises(ValueError):
            OrderProposal(
                proposal_id=uid(),
                market_id="X",
                platform=Platform.POLYMARKET,
                side=Side.BUY_YES,
                size_usdc=50.0,
                limit_price=0.50,
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.ARB,
                # Missing leg_group_id and leg_number
                expiry_ms=now_ms() + 1000,
                source_ts=now_ms(),
            )

    def test_execution_result_fill_price_required(self):
        with self.assertRaises(ValueError):
            ExecutionResult(
                proposal_id=uid(),
                exchange_order_id="x",
                status=OrderStatus.PARTIAL,
                ts=now_ms(),
                filled_size_usdc=10.0,
                fill_price=None,  # required for PARTIAL
            )


# ═════════════════════════════════════════════════════════════════════════════
# 2. ORDER TRACKER
# ═════════════════════════════════════════════════════════════════════════════


class TestOrderTracker(unittest.TestCase):
    def _tracker(self, size=100.0, price=0.50):
        sub = make_submission(size=size, price=price)
        return OrderTracker(sub)

    def test_initial_state(self):
        t = self._tracker()
        self.assertEqual(t.status, TrackerStatus.AWAITING)
        self.assertEqual(t.fill_ratio, 0.0)
        self.assertIsNone(t.weighted_avg_price)

    def test_record_submission(self):
        t = self._tracker()
        result = t.record_submission("exch-001")
        self.assertEqual(t.status, TrackerStatus.SUBMITTED)
        self.assertEqual(result.status, OrderStatus.SUBMITTED)

    def test_partial_fill(self):
        t = self._tracker(size=100.0)
        t.record_submission("x")
        result = t.record_fill(40.0, 0.50, 80.0, now_ms())
        self.assertEqual(result.status, OrderStatus.PARTIAL)
        self.assertAlmostEqual(result.fill_ratio, 0.40)
        self.assertAlmostEqual(t.remaining_usdc, 60.0)

    def test_full_fill(self):
        t = self._tracker(size=100.0)
        t.record_submission("x")
        result = t.record_fill(100.0, 0.50, 200.0, now_ms())
        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertTrue(t.status.is_terminal)

    def test_dust_tolerance(self):
        t = self._tracker(size=100.0)
        t.record_submission("x")
        result = t.record_fill(99.95, 0.50, 199.9, now_ms())
        self.assertEqual(result.status, OrderStatus.FILLED, "99.95/100 = 0.9995 ≥ threshold → should be FILLED")

    def test_weighted_avg_price(self):
        t = self._tracker(size=100.0)
        t.record_submission("x")
        t.record_fill(30.0, 0.48, 62.5, now_ms())
        t.record_fill(70.0, 0.52, 134.6, now_ms())
        expected = (30 * 0.48 + 70 * 0.52) / 100
        self.assertAlmostEqual(t.weighted_avg_price, expected, places=6)

    def test_slippage_bps(self):
        t = self._tracker(size=100.0, price=0.50)
        t.record_submission("x")
        t.record_fill(100.0, 0.52, 192.3, now_ms())
        # |0.52 - 0.50| / 0.50 * 10000 = 400 bps
        self.assertEqual(t.slippage_bps, 400)

    def test_expiry(self):
        sub = make_submission()
        sub = OrderSubmission(
            order_id=sub.order_id,
            proposal_id=sub.proposal_id,
            market_id=sub.market_id,
            platform=sub.platform,
            side=sub.side,
            size_usdc=sub.size_usdc,
            limit_price=sub.limit_price,
            order_type=sub.order_type,
            strategy_id=sub.strategy_id,
            expiry_ms=now_ms() + 100,  # expires in 100ms
            token_quantity=sub.token_quantity,
            submitted_at=sub.submitted_at,
        )
        t = OrderTracker(sub)
        t.record_submission("x")
        self.assertFalse(t.is_expired(now_ms()))
        self.assertTrue(t.is_expired(now_ms() + 200))

    def test_cancellation_terminal(self):
        t = self._tracker()
        t.record_submission("x")
        result = t.record_cancellation()
        self.assertEqual(result.status, OrderStatus.CANCELLED)
        self.assertTrue(t.status.is_terminal)

    def test_rejection_terminal(self):
        t = self._tracker()
        t.record_submission("x")
        result = t.record_rejection("bad price")
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertEqual(result.exchange_error, "bad price")

    def test_cannot_fill_after_terminal(self):
        t = self._tracker()
        t.record_submission("x")
        t.record_cancellation()
        with self.assertRaises(AssertionError):
            t.record_fill(50.0, 0.50, 100.0, now_ms())


# ═════════════════════════════════════════════════════════════════════════════
# 3. PORTFOLIO MANAGER
# ═════════════════════════════════════════════════════════════════════════════


class TestPortfolioManager(unittest.TestCase):
    def test_initial_equity(self):
        pm = make_portfolio(cash=5_000.0)
        mtm = pm.get_portfolio_mtm()
        self.assertAlmostEqual(mtm.total_cash_usdc, 5_000.0)
        self.assertAlmostEqual(mtm.total_equity_usdc, 5_000.0)

    def test_record_fill_decreases_cash(self):
        pm = make_portfolio(cash=1_000.0)
        run_async(pm.start())
        fill = FillRecord(
            proposal_id=uid(),
            order_id=uid(),
            market_id="X",
            platform=Platform.POLYMARKET,
            side="buy_yes",
            filled_usdc=100.0,
            fill_price=0.50,
            ts=now_ms(),
        )
        run_async(pm.record_fill(fill))
        self.assertAlmostEqual(pm.cash_usdc, 900.0)
        run_async(pm.stop())

    def test_get_delta_after_buy(self):
        pm = make_portfolio()
        run_async(pm.start())
        run_async(
            pm.record_fill(
                FillRecord(
                    proposal_id=uid(),
                    order_id=uid(),
                    market_id="BTC",
                    platform=Platform.POLYMARKET,
                    side="buy_yes",
                    filled_usdc=100.0,
                    fill_price=0.50,
                    ts=now_ms(),
                )
            )
        )
        delta = pm.get_delta("BTC")
        # 100 / 0.50 = 200 tokens
        self.assertAlmostEqual(delta.net_delta, 200.0)
        run_async(pm.stop())

    def test_delta_neutral_after_yes_and_no(self):
        pm = make_portfolio()
        run_async(pm.start())
        for side in ["buy_yes", "buy_no"]:
            run_async(
                pm.record_fill(
                    FillRecord(
                        proposal_id=uid(),
                        order_id=uid(),
                        market_id="BTC",
                        platform=Platform.POLYMARKET,
                        side=side,
                        filled_usdc=100.0,
                        fill_price=0.50,
                        ts=now_ms(),
                    )
                )
            )
        delta = pm.get_delta("BTC")
        self.assertAlmostEqual(delta.net_delta, 0.0)
        run_async(pm.stop())

    def test_negative_holdings_raises(self):
        pm = make_portfolio()
        run_async(pm.start())
        run_async(
            pm.record_fill(
                FillRecord(
                    proposal_id=uid(),
                    order_id=uid(),
                    market_id="BTC",
                    platform=Platform.POLYMARKET,
                    side="buy_yes",
                    filled_usdc=50.0,
                    fill_price=0.50,
                    ts=now_ms(),
                )
            )
        )
        with self.assertRaises(NegativeHoldings):
            run_async(
                pm.record_fill(
                    FillRecord(
                        proposal_id=uid(),
                        order_id=uid(),
                        market_id="BTC",
                        platform=Platform.POLYMARKET,
                        side="sell_yes",
                        filled_usdc=200.0,
                        fill_price=0.55,
                        ts=now_ms(),
                    )
                )
            )
        run_async(pm.stop())

    def test_capital_reservation_sync(self):
        pm = make_portfolio(cash=1_000.0)
        run_async(pm.reserve_capital(300.0))
        self.assertAlmostEqual(pm.available_capital, 700.0)
        run_async(pm.release_capital(300.0))
        self.assertAlmostEqual(pm.available_capital, 1_000.0)


# ═════════════════════════════════════════════════════════════════════════════
# 4. KILL SWITCH
# ═════════════════════════════════════════════════════════════════════════════


class TestKillSwitch(unittest.TestCase):
    def test_initially_inactive(self):
        ks = KillSwitch("test-token-secure-123")
        self.assertFalse(ks.is_active)

    def test_activate(self):
        ks = KillSwitch("test-token-secure-123")
        r = ks.activate("test", 0.20, 10000.0, 8000.0, "prop-1")
        self.assertTrue(ks.is_active)
        self.assertEqual(r.triggering_id, "prop-1")
        self.assertEqual(ks.activation_count, 1)

    def test_correct_token_resets(self):
        ks = KillSwitch("test-token-secure-123")
        ks.activate("t", 0.20, 10000.0, 8000.0)
        self.assertTrue(ks.reset("test-token-secure-123", "ops"))
        self.assertFalse(ks.is_active)

    def test_wrong_token_blocked(self):
        ks = KillSwitch("test-token-secure-123")
        ks.activate("t", 0.20, 10000.0, 8000.0)
        self.assertFalse(ks.reset("wrong-token-999"))
        self.assertTrue(ks.is_active)

    def test_empty_token_raises(self):
        with self.assertRaises(ValueError):
            KillSwitch("")

    def test_audit_trail(self):
        ks = KillSwitch("test-token-secure-123")
        ks.activate("r1", 0.20, 10000, 8000)
        ks.reset("test-token-secure-123", "op1")
        ks.activate("r2", 0.22, 10000, 7800)
        trail = ks.audit_trail()
        self.assertEqual(trail["activation_count"], 2)
        self.assertEqual(trail["reset_count"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# 5. RISK ENGINE — synchronous capital reservation (critical TOCTOU fix)
# ═════════════════════════════════════════════════════════════════════════════


class TestRiskEngine(unittest.TestCase):
    def test_approve_valid_proposal(self):
        # size=20 at price=0.50 → 40 tokens < 50 delta limit → approved
        risk, _, _ = make_risk()
        d = risk.evaluate(make_proposal(size=20.0))
        self.assertTrue(d.approved, f"Expected APPROVED, got {d.reject_reason}: {d.reject_detail}")
        self.assertIsNone(d.reject_reason)

    def test_toctou_fix_second_proposal_sees_reservation(self):
        """
        Critical audit fix: both proposals evaluated BEFORE any async task.
        Second proposal must see reduced available capital.

        Limits disable market-exposure and delta checks so that the ONLY
        binding constraint is capital availability.
        """
        # Disable market-exposure, delta and liquidity-buffer checks so that
        # the ONLY binding constraint is capital availability. This isolates
        # the TOCTOU race-condition fix (synchronous reservation).
        lim = RiskLimits(
            min_free_capital_pct=0.0,
            max_single_order_usdc=700.0,
            max_market_exposure_usdc=10_000.0,
            max_market_exposure_pct=1.0,  # disable pct-based cap
            max_net_delta_per_market=10_000.0,
        )
        risk, _, _ = make_risk(make_portfolio(cash=1_000.0), lim)

        p1 = make_proposal(size=600.0)
        p2 = make_proposal(size=600.0)

        d1 = risk.evaluate(p1)
        d2 = risk.evaluate(p2)  # Must see $400 available (1000 - 600), not $1000

        self.assertTrue(d1.approved, f"First proposal should be approved, got {d1.reject_reason}: {d1.reject_detail}")
        self.assertTrue(d2.rejected, "Second proposal must be rejected — capital already reserved")
        self.assertIn(
            d2.reject_reason,
            (
                RejectReason.INSUFFICIENT_CAPITAL,
                RejectReason.LIQUIDITY_BUFFER,
            ),
            f"Expected capital exhaustion, got {d2.reject_reason}",
        )

    def test_reservation_immediate(self):
        """After approve, reserved capital is visible to next evaluate() synchronously."""
        lim = RiskLimits(min_free_capital_pct=0.0, max_single_order_usdc=500.0)
        risk, _, _ = make_risk(make_portfolio(cash=500.0), lim)
        risk.evaluate(make_proposal(size=400.0))
        d2 = risk.evaluate(make_proposal(size=200.0))
        self.assertTrue(d2.rejected)
        # 500 cash - 400 reserved = 100 available < 200 requested

    def test_terminal_release(self):
        """After notify_terminal(), reserved capital is released and next proposal is approved."""
        lim = RiskLimits(
            min_free_capital_pct=0.0,
            max_single_order_usdc=700.0,
            max_market_exposure_usdc=10_000.0,
            max_market_exposure_pct=1.0,  # disable pct-based cap
            max_net_delta_per_market=10_000.0,
        )
        pm = make_portfolio(cash=1_000.0)
        run_async(pm.start())
        risk, _, _ = make_risk(pm, lim)
        p1 = make_proposal(size=600.0)
        d1 = risk.evaluate(p1)
        self.assertTrue(d1.approved, f"Expected APPROVED, got {d1.reject_reason}: {d1.reject_detail}")
        # Release the reservation
        run_async(risk.notify_terminal(p1.proposal_id, Platform.POLYMARKET, 600.0))
        # Now same-size proposal should be approved again
        d2 = risk.evaluate(make_proposal(size=600.0))
        self.assertTrue(
            d2.approved, f"After terminal, capacity should be restored. Got {d2.reject_reason}: {d2.reject_detail}"
        )
        run_async(pm.stop())

    def test_kill_switch_blocks(self):
        risk, ks, _ = make_risk()
        ks.activate("t", 0.20, 10000, 8000)
        d = risk.evaluate(make_proposal(size=1.0))
        self.assertTrue(d.rejected)
        self.assertEqual(d.reject_reason, RejectReason.KILL_SWITCH_ACTIVE)

    def test_drawdown_triggers_kill_switch(self):
        lim = RiskLimits(drawdown_kill_pct=0.10, drawdown_warn_pct=0.05)
        pm = make_portfolio(cash=900.0)
        pm._peak_equity = 1_000.0  # simulate prior peak
        risk, ks, _ = make_risk(pm, lim)
        d = risk.evaluate(make_proposal(size=1.0))
        self.assertTrue(d.rejected)
        self.assertEqual(d.reject_reason, RejectReason.DRAWDOWN_LIMIT)
        self.assertTrue(ks.is_active)

    def test_get_portfolio_mtm_synchronous_lock(self):
        """
        P0-001 regression: get_portfolio_mtm() must use sync lock.
        RiskEngine.evaluate() calls PortfolioManager.get_portfolio_mtm()
        synchronously, so it cannot use asyncio.Lock with 'with'.
        """
        lim = RiskLimits(
            max_net_delta_per_market=10_000.0,
            min_free_capital_pct=0.0,
            max_market_exposure_usdc=10_000.0,
            max_market_exposure_pct=1.0,
        )
        pm = make_portfolio(cash=5_000.0)
        # Add a position to ensure MTM computation is non-trivial
        run_async(
            pm.record_fill(
                FillRecord(
                    proposal_id=uid(),
                    order_id=uid(),
                    market_id="TEST",
                    platform=Platform.POLYMARKET,
                    side="buy_yes",
                    filled_usdc=100.0,
                    fill_price=0.50,
                    ts=now_ms(),
                )
            )
        )
        risk, _, _ = make_risk(pm, lim)

        # This must NOT raise TypeError about Lock context manager
        mtm = pm.get_portfolio_mtm()
        self.assertGreater(mtm.total_equity_usdc, 0.0)

        # RiskEngine.evaluate() internally calls get_portfolio_mtm()
        # so this exercises the actual call chain
        d = risk.evaluate(make_proposal(size=100.0))
        self.assertTrue(d.approved, f"Expected APPROVED, got {d.reject_reason}: {d.reject_detail}")

    def test_projected_delta_check(self):
        """Check 12: uses PROJECTED delta, not current."""
        lim = RiskLimits(max_net_delta_per_market=50.0, min_free_capital_pct=0.0)
        pm = make_portfolio(cash=10_000.0)
        run_async(pm.start())
        # Buy YES to get delta = +80 tokens (40 USDC / 0.50 = 80 tokens)
        run_async(
            pm.record_fill(
                FillRecord(
                    proposal_id=uid(),
                    order_id=uid(),
                    market_id="BTC",
                    platform=Platform.POLYMARKET,
                    side="buy_yes",
                    filled_usdc=40.0,
                    fill_price=0.50,
                    ts=now_ms(),
                )
            )
        )
        risk, _, _ = make_risk(pm, lim)
        # 10 USDC / 0.50 = 20 more tokens → projected = 80+20 = 100 > 50
        d = risk.evaluate(make_proposal(size=10.0, price=0.50, market="BTC"))
        self.assertTrue(d.rejected)
        self.assertEqual(d.reject_reason, RejectReason.DELTA_LIMIT)
        run_async(pm.stop())

    def test_buy_no_reduces_delta_allowed(self):
        """Buying NO reduces delta — should pass check 12."""
        lim = RiskLimits(max_net_delta_per_market=50.0, min_free_capital_pct=0.0)
        pm = make_portfolio(cash=10_000.0)
        run_async(pm.start())
        run_async(
            pm.record_fill(
                FillRecord(
                    proposal_id=uid(),
                    order_id=uid(),
                    market_id="BTC",
                    platform=Platform.POLYMARKET,
                    side="buy_yes",
                    filled_usdc=20.0,
                    fill_price=0.50,
                    ts=now_ms(),
                )
            )
        )
        # delta = +40 tokens
        risk, _, _ = make_risk(pm, lim)
        # Buy NO: 10/0.50 = 20 tokens → projected = 40-20 = 20 < 50 ✓
        d = risk.evaluate(make_proposal(size=10.0, price=0.50, side=Side.BUY_NO, market="BTC"))
        self.assertTrue(d.approved)
        run_async(pm.stop())


# ═════════════════════════════════════════════════════════════════════════════
# 6. AI SIGNAL CONTEXT AND HEURISTIC
# ═════════════════════════════════════════════════════════════════════════════


class TestSignalContext(unittest.TestCase):
    def test_valid_construction(self):
        ctx = SignalContext(
            market_id="X",
            confidence_multiplier=1.2,
            regime=MarketRegime.STABLE,
            vol_regime=VolRegime.NORMAL,
            suppress_mm=False,
            arb_quality=0.8,
            hedge_urgency=0.1,
            model_version="v1",
            inference_ms=5.0,
            feature_count=20,
            is_fallback=False,
        )
        self.assertAlmostEqual(ctx.effective_arb_multiplier, 0.8 * 1.2)

    def test_confidence_out_of_range(self):
        with self.assertRaises(ValueError):
            SignalContext(
                market_id="X",
                confidence_multiplier=0.0,
                regime=MarketRegime.UNKNOWN,
                vol_regime=VolRegime.NORMAL,
                suppress_mm=False,
                arb_quality=0.5,
                hedge_urgency=0.0,
                model_version="v",
                inference_ms=0,
                feature_count=0,
                is_fallback=True,
            )

    def test_total_blackout_prevented(self):
        """Cannot suppress both arb and MM simultaneously."""
        with self.assertRaises(ValueError):
            SignalContext(
                market_id="X",
                confidence_multiplier=CONFIDENCE_MIN,
                regime=MarketRegime.THIN,
                vol_regime=VolRegime.SPIKE,
                suppress_mm=True,
                arb_quality=0.0,
                hedge_urgency=0.0,
                model_version="v",
                inference_ms=0,
                feature_count=0,
                is_fallback=True,
            )

    def test_is_urgent_hedge(self):
        ctx = SignalContext(
            market_id="X",
            confidence_multiplier=1.0,
            regime=MarketRegime.UNKNOWN,
            vol_regime=VolRegime.NORMAL,
            suppress_mm=False,
            arb_quality=0.5,
            hedge_urgency=0.85,
            model_version="v",
            inference_ms=0,
            feature_count=0,
            is_fallback=True,
        )
        self.assertTrue(ctx.is_urgent_hedge)


class TestHeuristic(unittest.TestCase):
    def test_returns_signal_context(self):
        ctx = heuristic_enhance(make_fv())
        self.assertIsInstance(ctx, SignalContext)
        self.assertTrue(ctx.is_fallback)

    def test_thin_regime_on_wide_spread(self):
        fv = make_fv(spread_pm=0.10, spread_op=0.10)
        ctx = heuristic_enhance(fv)
        self.assertEqual(ctx.regime, MarketRegime.THIN)

    def test_zero_arb_quality_on_nan(self):
        fv = make_fv(stale=[Platform.POLYMARKET])
        ctx = heuristic_enhance(fv)
        self.assertEqual(ctx.arb_quality, 0.0)

    def test_suppress_mm_near_resolution(self):
        fv = make_fv(days=1.0)
        ctx = heuristic_enhance(fv)
        self.assertTrue(ctx.suppress_mm)

    def test_hedge_urgency_zero_when_flat(self):
        ctx = heuristic_enhance(make_fv(delta=0.0))
        self.assertAlmostEqual(ctx.hedge_urgency, 0.0)

    def test_hedge_urgency_max_on_large_delta(self):
        ctx = heuristic_enhance(make_fv(delta=100.0))
        self.assertAlmostEqual(ctx.hedge_urgency, 1.0)

    def test_never_raises_on_edge_cases(self):
        edge_cases = [
            make_fv(mid_pm=0.001, mid_op=0.999),
            make_fv(vol_30s=None),
            make_fv(days=None),
            make_fv(delta=1000.0),
            make_fv(bid_pm=0.0, ask_pm=0.0),
        ]
        for fv in edge_cases:
            ctx = heuristic_enhance(fv)  # must not raise
            self.assertIsInstance(ctx, SignalContext)


# ═════════════════════════════════════════════════════════════════════════════
# 7. ARBITRAGE STRATEGY
# ═════════════════════════════════════════════════════════════════════════════


class TestArbitrageStrategy(unittest.TestCase):
    def _strat(self, **kw):
        return ArbitrageStrategy(config=ArbConfig(**kw))

    def test_nan_signal_rejected(self):
        strat = self._strat()
        result = strat.evaluate(make_fv(stale=[Platform.POLYMARKET]))
        self.assertFalse(result.accepted)
        self.assertIn("stale", result.rejection_reason)

    def test_no_edge_rejected(self):
        strat = self._strat()
        fv = make_fv(mid_pm=0.50, mid_op=0.50)  # symmetric
        result = strat.evaluate(fv)
        self.assertFalse(result.accepted)

    def test_wide_spread_rejected(self):
        strat = self._strat(max_spread_fraction=0.03)
        fv = make_fv(spread_pm=0.10)  # 10/0.50 = 20% >> 3%
        result = strat.evaluate(fv)
        self.assertFalse(result.accepted)
        self.assertIn("spread", result.rejection_reason)

    def test_thin_book_rejected(self):
        strat = self._strat(min_order_usdc=10.0)
        fv = make_fv(mid_pm=0.35, mid_op=0.72, ask_pm=5.0, ask_op=5.0)
        result = strat.evaluate(fv)
        self.assertFalse(result.accepted)

    def test_profitable_arb_accepted(self):
        strat = self._strat(min_net_edge=0.001, max_signal_age_ms=60_000)
        fv = make_fv(
            mid_pm=0.35,
            mid_op=0.72,
            spread_pm=0.01,
            spread_op=0.01,
            ask_pm=2000.0,
            ask_op=1500.0,
        )
        result = strat.evaluate(fv)
        self.assertTrue(result.accepted, f"rejected: {result.rejection_reason}")
        self.assertIsNotNone(result.leg1_proposal)
        self.assertIsNotNone(result.leg2_proposal)

    def test_arb_cross_venue(self):
        strat = self._strat(min_net_edge=0.001, max_signal_age_ms=60_000)
        fv = make_fv(mid_pm=0.35, mid_op=0.72, ask_pm=2000.0, ask_op=1500.0)
        result = strat.evaluate(fv)
        if result.accepted:
            self.assertNotEqual(
                result.leg1_proposal.platform,
                result.leg2_proposal.platform,
                "Arb legs must be on different venues",
            )

    def test_net_edge_less_than_gross(self):
        """Slippage reduces net edge below fee-adjusted arb_signal."""
        strat = self._strat(min_net_edge=0.001, max_signal_age_ms=60_000)
        fv = make_fv(mid_pm=0.35, mid_op=0.72, ask_pm=2000.0, ask_op=1500.0)
        result = strat.evaluate(fv)
        if result.accepted:
            self.assertLess(result.net_edge, fv.arb_signal)

    def test_cost_estimate_sqrt_scaling(self):
        """Deeper books → lower impact cost."""
        c_shallow = estimate_taker_cost(100.0, 0.46, 0.44, 100.0, 20)
        c_deep = estimate_taker_cost(100.0, 0.46, 0.44, 5000.0, 20)
        self.assertLess(c_deep.impact_usdc, c_shallow.impact_usdc)


# ═════════════════════════════════════════════════════════════════════════════
# 8. FILL SIMULATOR
# ═════════════════════════════════════════════════════════════════════════════


class TestFillSimulator(unittest.TestCase):
    def setUp(self):
        import random

        random.seed(42)
        self.sim = FillSimulator(LatencyModel())

    def _prop(self, side=Side.BUY_YES, price=0.50, size=50.0, expiry_ms=None):
        return make_proposal(side=side, price=price, size=size)

    def test_no_fill_when_not_crossing(self):
        prop = self._prop(Side.BUY_YES, price=0.40)  # limit below ask
        self.sim.submit(prop, 1000)
        snap = make_snapshot(yes_bid=0.44, yes_ask=0.46, ask_depth=500.0)
        events = self.sim.process_tick(snap, 1100)
        fills = [e for e in events if e.filled_usdc > 0]
        self.assertEqual(len(fills), 0)

    def test_fill_when_crossing(self):
        prop = self._prop(Side.BUY_YES, price=0.50)  # limit above ask=0.46
        self.sim.submit(prop, 1000)
        snap = make_snapshot(yes_bid=0.44, yes_ask=0.46, ask_depth=500.0)
        events = self.sim.process_tick(snap, 1100)
        fills = [e for e in events if e.filled_usdc > 0]
        self.assertGreater(len(fills), 0)
        if fills:
            # fill_price includes slippage impact, so it's >= ask
            self.assertGreaterEqual(fills[0].fill_price, 0.46)

    def test_partial_when_order_exceeds_depth(self):
        prop = self._prop(Side.BUY_YES, price=0.50, size=10_000.0)
        self.sim.submit(prop, 1000)
        snap = make_snapshot(yes_bid=0.44, yes_ask=0.46, ask_depth=100.0)
        events = self.sim.process_tick(snap, 1100)
        fills = [e for e in events if e.filled_usdc > 0]
        if fills:
            self.assertLessEqual(fills[0].filled_usdc, 100.0 + 0.01)

    def test_expiry_cancels_order(self):
        t = now_ms()
        sub = OrderSubmission(
            order_id=uid(),
            proposal_id=uid(),
            market_id="BTC-Q4",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=50.0,
            limit_price=0.30,
            order_type=OrderType.LIMIT,
            strategy_id=StrategyId.MM,
            expiry_ms=t + 100,
            token_quantity=round(50.0 / 0.30, 6),
            submitted_at=t,
        )
        p = OrderProposal(
            proposal_id=sub.proposal_id,
            market_id=sub.market_id,
            platform=sub.platform,
            side=sub.side,
            size_usdc=sub.size_usdc,
            limit_price=sub.limit_price,
            order_type=sub.order_type,
            strategy_id=sub.strategy_id,
            expiry_ms=sub.expiry_ms,
            source_ts=t,
        )
        self.sim.submit(p, t)
        snap = make_snapshot(yes_bid=0.44, yes_ask=0.46)
        events = self.sim.process_tick(snap, t + 200)
        expired = [e for e in events if e.status == OrderStatus.CANCELLED]
        self.assertEqual(len(expired), 1)

    def test_wrong_market_no_fill(self):
        prop = self._prop(Side.BUY_YES, price=0.50)
        self.sim.submit(prop, 1000)
        snap = make_snapshot("OTHER-MARKET", yes_bid=0.44, yes_ask=0.46)
        events = self.sim.process_tick(snap, 1100)
        self.assertEqual(len(events), 0)

    def test_slippage_computed(self):
        prop = self._prop(Side.BUY_YES, price=0.50)
        self.sim.submit(prop, 1000)
        snap = make_snapshot(yes_bid=0.44, yes_ask=0.46, ask_depth=500.0)
        events = self.sim.process_tick(snap, 1100)
        fills = [e for e in events if e.filled_usdc > 0]
        if fills:
            # fill_price includes slippage impact, so slippage_bps should be > 0
            self.assertIsNotNone(fills[0].slippage_bps)
            self.assertGreater(fills[0].slippage_bps, 0, "Expected positive slippage")


# ═════════════════════════════════════════════════════════════════════════════
# 9. BACKTEST ENGINE END-TO-END
# ═════════════════════════════════════════════════════════════════════════════


class TestBacktestEngine(unittest.TestCase):
    def _run(self, n=50, capital=5000.0, seed=42, **kw):
        ticks = build_synthetic_tick_stream("BTC-Q4", n_ticks=n, seed=seed)
        engine = BacktestEngine(
            tick_streams={"BTC-Q4": ticks},
            initial_capital=capital,
            seed=seed,
            **kw,
        )
        return run_async(engine.run())

    def test_runs_to_completion(self):
        result = self._run(n=50)
        self.assertIsNotNone(result)
        self.assertEqual(result.total_ticks, 50)

    def test_approved_plus_rejected_equals_total(self):
        result = self._run(n=100)
        self.assertEqual(
            result.approved_count + result.rejected_count,
            result.total_proposals,
        )

    def test_zero_pnl_when_all_rejected(self):
        result = self._run(
            n=50,
            risk_limits=RiskLimits(
                min_single_order_usdc=999_999.0,
                max_single_order_usdc=1_000_000.0,
                min_free_capital_pct=0.0,
            ),
        )
        self.assertAlmostEqual(result.total_pnl, 0.0, places=4)
        self.assertAlmostEqual(result.max_drawdown, 0.0, places=4)

    def test_fill_rate_in_bounds(self):
        result = self._run(n=100)
        self.assertGreaterEqual(result.fill_rate, 0.0)
        self.assertLessEqual(result.fill_rate, 1.0)

    def test_equity_series_starts_at_capital(self):
        result = self._run(n=200, capital=5_000.0)
        self.assertGreaterEqual(len(result.equity_series), 2)
        self.assertAlmostEqual(result.equity_series[0][1], 5_000.0, delta=5.0)

    def test_multi_market(self):
        ticks_a = build_synthetic_tick_stream("BTC-Q4", n_ticks=30, seed=1)
        ticks_b = build_synthetic_tick_stream("ETH-Q1", n_ticks=30, seed=2)
        engine = BacktestEngine(
            tick_streams={"BTC-Q4": ticks_a, "ETH-Q1": ticks_b},
            initial_capital=5_000.0,
            seed=3,
        )
        result = run_async(engine.run())
        self.assertEqual(result.total_ticks, 60)
        self.assertIn("BTC-Q4", result.market_ids)
        self.assertIn("ETH-Q1", result.market_ids)

    def test_summary_contains_expected_fields(self):
        result = self._run(n=50)
        summary = result.summary()
        for word in ["P&L", "Drawdown", "Sharpe", "Fill rate", "ticks"]:
            self.assertIn(word, summary)


class TestSyntheticData(unittest.TestCase):
    def test_correct_length(self):
        ticks = build_synthetic_tick_stream("X", n_ticks=100, seed=1)
        self.assertEqual(len(ticks), 100)

    def test_prices_in_bounds(self):
        ticks = build_synthetic_tick_stream("X", n_ticks=100, seed=2)
        for _, pm, op in ticks:
            self.assertLess(pm.yes_bid, pm.yes_ask)
            self.assertGreater(pm.yes_bid, 0)
            self.assertLess(op.yes_bid, op.yes_ask)

    def test_timestamps_monotonic(self):
        ticks = build_synthetic_tick_stream("X", n_ticks=50, seed=3)
        ts_list = [t for t, _, _ in ticks]
        self.assertEqual(ts_list, sorted(ts_list))

    def test_reproducible_with_seed(self):
        # Use a fixed start_ts_ms so both calls produce identical timestamps
        fixed_ts = 1_700_000_000_000
        a = build_synthetic_tick_stream("X", n_ticks=10, seed=99, start_ts_ms=fixed_ts)
        b = build_synthetic_tick_stream("X", n_ticks=10, seed=99, start_ts_ms=fixed_ts)
        for (ta, pa, oa), (tb, pb, ob) in zip(a, b):
            self.assertEqual(ta, tb)
            self.assertAlmostEqual(pa.yes_bid, pb.yes_bid, places=10)


class TestStatistics(unittest.TestCase):
    def test_max_drawdown_zero_no_series(self):
        self.assertEqual(_max_drawdown([]), 0.0)

    def test_max_drawdown_computes_correctly(self):
        series = [(0, 1000), (1, 900), (2, 1100), (3, 850)]
        # Peak=1100 at t=2, then 850 → drawdown = (1100-850)/1100 ≈ 0.227
        dd = _max_drawdown(series)
        self.assertAlmostEqual(dd, 250 / 1100, places=4)

    def test_sharpe_none_for_short_series(self):
        self.assertIsNone(_sharpe([(0, 100), (1, 101)]))

    def test_sharpe_computes(self):
        import random as r

        r.seed(1)
        eq = 10000.0
        series = [(i * 1000, eq * (1 + r.gauss(0.0001, 0.001))) for i in range(50)]
        result = _sharpe(series)
        # May be None if std=0, otherwise should be a float
        self.assertTrue(result is None or isinstance(result, float))


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
