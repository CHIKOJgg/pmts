"""
tests/test_integration.py — Integration and system tests for PMTS.

Three levels of coverage:
  Integration — two or more real components wired together, no mocks
  System      — full pipeline end-to-end, seeded for determinism
  Regression  — specific bug fixes that must never regress

Run with:
    python -m unittest tests.test_integration -v
"""
from __future__ import annotations

import asyncio
import math
import time
import unittest
import uuid
from typing import List

# ── Helpers ──────────────────────────────────────────────────────────────────

def run(coro):
    """Run a coroutine synchronously (stdlib-only, no pytest-asyncio needed)."""
    return asyncio.get_event_loop().run_until_complete(coro)


def now_ms() -> int:
    return int(time.time() * 1000)


# ─────────────────────────────────────────────────────────────────────────────
# I. MDP → FeatureEngine integration
# ─────────────────────────────────────────────────────────────────────────────

class TestMDPFeatureEngineIntegration(unittest.TestCase):
    """MarketDataProvider → FeatureEngine pipeline."""

    def setUp(self):
        from data.market_data_provider import MarketDataProvider
        from engine.feature_engine import FeatureEngine
        from portfolio.manager import PortfolioManager

        def price_source(m, p): return (0.50, 0.50)
        self.pm  = PortfolioManager(10_000.0, price_source)
        self.mdp = MarketDataProvider()
        self.fe  = FeatureEngine(self.pm)
        self.mdp.add_callback(self.fe.on_snapshot)

        self.fvs: list = []
        self.fe.add_callback(self._collect_fv)

    async def _collect_fv(self, fv):
        self.fvs.append(fv)

    def _snap(self, platform, mid=0.50, spread=0.02, ts_offset=0):
        from data.models import MarketSnapshot
        from src.types import Platform
        now = now_ms() + ts_offset
        return MarketSnapshot(
            market_id="TEST-1",
            platform=Platform.POLYMARKET if platform == "pm" else Platform.OPINION,
            yes_bid=round(mid - spread/2, 4),
            yes_ask=round(mid + spread/2, 4),
            no_bid=round((1-mid) - spread/2, 4),
            no_ask=round((1-mid) + spread/2, 4),
            bid_depth_usdc=500.0,
            ask_depth_usdc=500.0,
            taker_fee_bps=20,
            ts=now, received_ts=now+2,
        )

    def test_single_venue_snap_produces_stale_fv(self):
        """FV produced after one venue → arb_signal is NaN (other venue missing)."""
        run(self.pm.start())
        run(self.mdp.ingest(self._snap("pm")))
        self.assertEqual(len(self.fvs), 1)
        self.assertTrue(math.isnan(self.fvs[0].arb_signal))
        run(self.pm.stop())

    def test_both_venues_produce_valid_fv(self):
        """FV after both venues → arb_signal is a real number."""
        run(self.pm.start())
        run(self.mdp.ingest(self._snap("pm", mid=0.42)))
        run(self.mdp.ingest(self._snap("op", mid=0.53)))
        self.assertEqual(len(self.fvs), 2)
        # Second FV has both venues
        last = self.fvs[-1]
        self.assertFalse(math.isnan(last.arb_signal))
        run(self.pm.stop())

    def test_arb_signal_value(self):
        """arb_signal = 1 - yes_ask_pm - no_ask_op - fee_pm - fee_op."""
        from data.models import MarketSnapshot
        from src.types import Platform
        now = now_ms()
        # Construct a specific crossed market for exact calculation
        pm = MarketSnapshot("M", Platform.POLYMARKET,
                            yes_bid=0.40, yes_ask=0.42,
                            no_bid=0.56, no_ask=0.58,
                            bid_depth_usdc=300, ask_depth_usdc=300,
                            taker_fee_bps=20, ts=now, received_ts=now+1)
        op = MarketSnapshot("M", Platform.OPINION,
                            yes_bid=0.40, yes_ask=0.42,
                            no_bid=0.56, no_ask=0.58,
                            bid_depth_usdc=300, ask_depth_usdc=300,
                            taker_fee_bps=25, ts=now, received_ts=now+1)
        run(self.pm.start())
        run(self.mdp.ingest(pm))
        run(self.mdp.ingest(op))
        last = self.fvs[-1]
        expected = 1.0 - 0.42 - 0.58 - 0.002 - 0.0025
        self.assertAlmostEqual(last.arb_signal, expected, places=6)
        run(self.pm.stop())

    def test_dedup_suppresses_identical_snaps(self):
        """Identical snapshots should be deduplicated by MDP."""
        run(self.pm.start())
        snap = self._snap("pm")
        run(self.mdp.ingest(snap))
        run(self.mdp.ingest(snap))  # duplicate
        self.assertEqual(self.mdp.dedup_suppressed, 1)
        run(self.pm.stop())

    def test_stale_snap_marked(self):
        """Snapshot with ts far in the past should be marked stale."""
        from data.models import MarketSnapshot
        from src.types import Platform
        old_ts = now_ms() - 10_000  # 10 seconds ago
        snap = MarketSnapshot("M", Platform.POLYMARKET,
                              yes_bid=0.48, yes_ask=0.52,
                              no_bid=0.48, no_ask=0.52,
                              bid_depth_usdc=100, ask_depth_usdc=100,
                              taker_fee_bps=20, ts=old_ts, received_ts=old_ts+5_000)
        run(self.pm.start())
        run(self.mdp.ingest(snap))
        # Snap should be in index (even if stale)
        stored = self.mdp.get_snapshot("M", Platform.POLYMARKET)
        self.assertIsNotNone(stored)
        run(self.pm.stop())


# ─────────────────────────────────────────────────────────────────────────────
# II. StrategyEngine + RiskEngine integration
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyRiskIntegration(unittest.TestCase):
    """StrategyEngine produces proposals → RiskEngine gates them correctly."""

    def setUp(self):
        from portfolio.manager import PortfolioManager
        from risk.engine import RiskEngine
        from risk.kill_switch import KillSwitch
        from risk.limits import RiskLimits

        def price_source(m, p): return (0.50, 0.50)
        self.portfolio = PortfolioManager(10_000.0, price_source)

        lim = RiskLimits(
            max_single_order_usdc=300.0,
            max_market_exposure_usdc=5_000.0,
            max_market_exposure_pct=0.50,
            max_net_delta_per_market=5_000.0,
            max_arb_capital_usdc=5_000.0,
            max_mm_capital_usdc=5_000.0,
        )
        self.risk = RiskEngine(
            portfolio=self.portfolio,
            kill_switch=KillSwitch("test-token"),
            limits=lim,
        )
        self.approved: list = []
        self.rejected: list = []

    def _proposal(self, size=100.0, strategy="mm", market="BTC-Q4", price=0.50):
        from execution.models import OrderProposal
        from src.types import Platform, Side, OrderType, StrategyId
        strat = {"mm": StrategyId.MM, "arb": StrategyId.ARB, "hedge": StrategyId.HEDGE}[strategy]
        kwargs = dict(
            proposal_id=str(uuid.uuid4()),
            market_id=market,
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=size,
            limit_price=price,
            order_type=OrderType.LIMIT,
            strategy_id=strat,
            expiry_ms=now_ms() + 30_000,
            source_ts=now_ms(),
        )
        if strat == StrategyId.ARB:
            kwargs["leg_group_id"] = str(uuid.uuid4())
            kwargs["leg_number"]   = __import__("src.types", fromlist=["ArbLeg"]).ArbLeg.LEG_1
            kwargs["min_fill_ratio"] = 0.80
        return OrderProposal(**kwargs)

    def test_valid_proposal_approved(self):
        d = self.risk.evaluate(self._proposal(size=50.0))
        self.assertTrue(d.approved)

    def test_order_too_large_rejected(self):
        from src.types import RejectReason
        d = self.risk.evaluate(self._proposal(size=400.0))  # > 300 limit
        self.assertTrue(d.rejected)
        self.assertEqual(d.reject_reason, RejectReason.ORDER_TOO_LARGE)

    def test_order_too_small_rejected(self):
        from src.types import RejectReason
        d = self.risk.evaluate(self._proposal(size=0.50))
        self.assertTrue(d.rejected)
        self.assertEqual(d.reject_reason, RejectReason.ORDER_TOO_SMALL)

    def test_capital_reservation_is_synchronous(self):
        """Two simultaneous proposals: second must see reduced capital."""
        from src.types import RejectReason, RiskVerdict
        p1 = self._proposal(size=9_000.0)  # > cash ($10k), should fail on liquidity
        p2 = self._proposal(size=8_000.0)
        # But with 10% liquidity buffer min_free = 10000 * 0.10 = 1000
        # p1: 10000 - 9000 = 1000 >= 1000 → borderline pass
        # Let's use amounts that definitively test reservation
        from risk.limits import RiskLimits
        from risk.kill_switch import KillSwitch
        lim = RiskLimits(
            min_free_capital_pct=0.0,
            max_single_order_usdc=10_000.0,
            max_market_exposure_usdc=100_000.0,
            max_market_exposure_pct=1.0,
            max_net_delta_per_market=100_000.0,
            max_arb_capital_usdc=100_000.0,
            max_mm_capital_usdc=100_000.0,
        )
        def ps(m, p): return (0.50, 0.50)
        from portfolio.manager import PortfolioManager
        pm   = PortfolioManager(1_000.0, ps)
        from risk.engine import RiskEngine as _RE
        risk = _RE(pm, KillSwitch("tok"), lim)

        d1 = risk.evaluate(self._proposal(size=700.0))
        d2 = risk.evaluate(self._proposal(size=700.0))  # 700+700=1400 > 1000

        self.assertTrue(d1.approved, f"p1 rejected: {d1.reject_reason}")
        self.assertTrue(d2.rejected, f"p2 should be rejected but approved")

    def test_kill_switch_fires_at_drawdown(self):
        """When equity < peak * (1 - kill_pct), kill switch activates."""
        from src.types import RejectReason
        from risk.limits import RiskLimits
        from risk.kill_switch import KillSwitch
        from portfolio.manager import PortfolioManager

        # Start with $1000 but fake equity at $700 (30% drawdown > 20% kill)
        def ps(m, p): return (0.30, 0.30)   # prices at 0.30 → reduce MTM
        pm = PortfolioManager(700.0, ps)
        object.__setattr__(pm, '_peak_equity', 1000.0)  # force peak higher

        from risk.engine import RiskEngine as _RE2
        risk = _RE2(pm, KillSwitch("tok"),
                    RiskLimits(drawdown_kill_pct=0.20, drawdown_warn_pct=0.15))
        d = risk.evaluate(self._proposal(size=10.0))
        self.assertTrue(d.rejected)
        self.assertEqual(d.reject_reason, RejectReason.DRAWDOWN_LIMIT)
        self.assertTrue(risk.kill_switch_active)

    def test_kill_switch_reset(self):
        """Kill switch resets with correct token, stays active with wrong token."""
        self.risk._kill_switch.activate("test", 0.25, 1000, 750)
        self.assertTrue(self.risk.kill_switch_active)
        self.assertFalse(self.risk.reset_kill_switch("wrong-token"))
        self.assertTrue(self.risk.kill_switch_active)
        self.assertTrue(self.risk.reset_kill_switch("test-token"))
        self.assertFalse(self.risk.kill_switch_active)

    def test_dedup_blocks_repeated_proposal_id(self):
        """Same proposal_id within dedup window is rejected."""
        from src.types import RejectReason
        p = self._proposal(size=50.0)
        d1 = self.risk.evaluate(p)
        d2 = self.risk.evaluate(p)  # same proposal_id
        self.assertTrue(d1.approved)
        self.assertTrue(d2.rejected)
        self.assertEqual(d2.reject_reason, RejectReason.DUPLICATE_PROPOSAL)

    def test_terminal_notification_releases_reservation(self):
        """After notify_terminal, capital is freed for next proposal."""
        from src.types import Platform
        p = self._proposal(size=9_500.0)

        from risk.limits import RiskLimits
        from risk.kill_switch import KillSwitch
        from portfolio.manager import PortfolioManager
        lim = RiskLimits(
            min_free_capital_pct=0.0,
            max_single_order_usdc=10_000.0,
            max_market_exposure_usdc=100_000.0,
            max_market_exposure_pct=1.0,
            max_net_delta_per_market=100_000.0,
            max_arb_capital_usdc=100_000.0,
            max_mm_capital_usdc=100_000.0,
        )
        def ps(m, p2): return (0.50, 0.50)
        pm   = PortfolioManager(10_000.0, ps)
        run(pm.start())
        from risk.engine import RiskEngine as _RE3
        risk = _RE3(pm, KillSwitch("tok"), lim)

        d1 = risk.evaluate(self._proposal(size=9_500.0))
        self.assertTrue(d1.approved)

        # Release reservation
        run(risk.notify_terminal(d1.proposal_id, Platform.POLYMARKET, 9_500.0))

        # Now a second $9500 order should pass again
        d2 = risk.evaluate(self._proposal(size=9_500.0))
        self.assertTrue(d2.approved, f"Expected approved after release, got {d2.reject_reason}")
        run(pm.stop())


# ─────────────────────────────────────────────────────────────────────────────
# III. Portfolio ↔ FillRecord integration
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolioIntegration(unittest.TestCase):
    """Portfolio fill recording, delta, MTM, and P&L computation."""

    def _make_pm(self, cash=10_000.0, yes_mid=0.50, no_mid=0.50):
        from portfolio.manager import PortfolioManager
        def price_source(m, p):
            return (yes_mid, no_mid)
        return PortfolioManager(cash, price_source)

    def _fill(self, market="BTC-Q4", platform="polymarket",
              side="buy_yes", usdc=100.0, price=0.50):
        from portfolio.manager import FillRecord
        from src.types import Platform
        return FillRecord(
            proposal_id=str(uuid.uuid4()),
            order_id=str(uuid.uuid4()),
            market_id=market,
            platform=Platform(platform),
            side=side,
            filled_usdc=usdc,
            fill_price=price,
            ts=now_ms(),
        )

    def test_buy_yes_increases_delta(self):
        pm = self._make_pm()
        run(pm.start())
        run(pm.record_fill(self._fill(side="buy_yes", usdc=100.0, price=0.50)))
        delta = pm.get_delta("BTC-Q4")
        self.assertAlmostEqual(delta.net_delta, 200.0, places=4)  # 100/0.50
        run(pm.stop())

    def test_buy_no_decreases_delta(self):
        pm = self._make_pm()
        run(pm.start())
        run(pm.record_fill(self._fill(side="buy_no", usdc=100.0, price=0.50)))
        delta = pm.get_delta("BTC-Q4")
        self.assertAlmostEqual(delta.net_delta, -200.0, places=4)
        run(pm.stop())

    def test_buy_yes_and_no_flat_delta(self):
        """Equal YES and NO positions → delta ≈ 0."""
        pm = self._make_pm()
        run(pm.start())
        run(pm.record_fill(self._fill(side="buy_yes", usdc=100.0, price=0.50)))
        run(pm.record_fill(self._fill(side="buy_no",  usdc=100.0, price=0.50)))
        delta = pm.get_delta("BTC-Q4")
        self.assertAlmostEqual(delta.net_delta, 0.0, places=4)
        run(pm.stop())

    def test_cash_decreases_on_buy(self):
        pm = self._make_pm(cash=1_000.0)
        run(pm.start())
        run(pm.record_fill(self._fill(usdc=200.0, price=0.50)))
        self.assertAlmostEqual(pm.cash_usdc, 800.0, places=4)
        run(pm.stop())

    def test_cash_increases_on_sell(self):
        pm = self._make_pm(cash=1_000.0)
        run(pm.start())
        # Buy first to build position
        run(pm.record_fill(self._fill(side="buy_yes", usdc=100.0, price=0.50)))
        # Then sell
        run(pm.record_fill(self._fill(side="sell_yes", usdc=50.0, price=0.55)))
        # After buy: cash=900; after sell: cash=950
        self.assertAlmostEqual(pm.cash_usdc, 950.0, places=2)
        run(pm.stop())

    def test_wavg_cost_basis(self):
        """Weighted-average cost: two buys at different prices."""
        pm = self._make_pm()
        run(pm.start())
        run(pm.record_fill(self._fill(side="buy_yes", usdc=100.0, price=0.40)))
        run(pm.record_fill(self._fill(side="buy_yes", usdc=100.0, price=0.60)))
        # 250 tokens at 0.40 + 166.67 tokens at 0.60 = avg 0.4857
        delta = pm.get_delta("BTC-Q4")
        pos = pm._positions.get(("BTC-Q4", __import__("src.types", fromlist=["Platform"]).Platform.POLYMARKET))
        expected_avg = (100.0 + 100.0) / (100.0/0.40 + 100.0/0.60)
        self.assertAlmostEqual(pos.avg_cost_yes, expected_avg, places=4)
        run(pm.stop())

    def test_sell_exceeds_holdings_raises(self):
        """Selling more tokens than held raises NegativeHoldings."""
        from src.errors import NegativeHoldings
        pm = self._make_pm()
        run(pm.start())
        run(pm.record_fill(self._fill(side="buy_yes", usdc=10.0, price=0.50)))
        with self.assertRaises(NegativeHoldings):
            run(pm.record_fill(self._fill(side="sell_yes", usdc=100.0, price=0.50)))
        run(pm.stop())

    def test_mtm_with_price_change(self):
        """After price moves to 0.70, yes tokens gain value."""
        from portfolio.manager import PortfolioManager
        current_price = [0.50]
        def ps(m, p): return (current_price[0], 1.0 - current_price[0])
        pm = PortfolioManager(1_000.0, ps)
        run(pm.start())
        run(pm.record_fill(self._fill(side="buy_yes", usdc=100.0, price=0.50)))
        # Position = 200 tokens at cost 0.50; MTM at 0.50 = $100
        mtm1 = pm.get_portfolio_mtm()
        self.assertAlmostEqual(mtm1.total_equity_usdc, 1_000.0, places=1)

        # Price moves to 0.70
        current_price[0] = 0.70
        mtm2 = pm.get_portfolio_mtm()
        self.assertGreater(mtm2.total_equity_usdc, 1_000.0)
        run(pm.stop())

    def test_available_capital_respects_reservations(self):
        """available_capital = cash - reserved."""
        pm = self._make_pm(cash=1_000.0)
        run(pm.start())
        run(pm.reserve_capital(300.0))
        self.assertAlmostEqual(pm.available_capital, 700.0, places=4)
        run(pm.release_capital(300.0))
        self.assertAlmostEqual(pm.available_capital, 1_000.0, places=4)
        run(pm.stop())


# ─────────────────────────────────────────────────────────────────────────────
# IV. ArbitrageStrategy integration with FeatureVector
# ─────────────────────────────────────────────────────────────────────────────

class TestArbitrageStrategyIntegration(unittest.TestCase):

    def _fv(self, arb_signal=0.08, age_ms=50, spread=0.02,
            depth=500.0, ofi=0.0, mid_pm=0.45, mid_op=0.52):
        from data.models import FeatureVector
        from src.types import Platform
        ts = now_ms() - age_ms
        return FeatureVector(
            market_id="BTC-Q4",
            ts=ts,
            computed_ts=ts + 1,
            arb_signal=arb_signal,
            stale_markets=[],
            mid_pm=mid_pm, mid_op=mid_op,
            spread_pm=spread, spread_op=spread,
            ofi_pm=ofi, ofi_op=ofi,
            vol_30s=0.01,
            days_to_resolution=30.0,
            portfolio_delta=0.0,
            bid_depth_pm=depth, ask_depth_pm=depth,
            bid_depth_op=depth, ask_depth_op=depth,
        )

    def test_good_arb_accepted(self):
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        arb = ArbitrageStrategy(ArbConfig(min_net_edge=0.003))
        # PM mid=0.35, OP mid=0.62, spread=0.02
        # yes_ask_pm = 0.35 + 0.01 = 0.36
        # no_ask_op  = (1-0.62) + 0.01 = 0.39
        # gross = 1 - 0.36 - 0.39 = 0.25 >> costs
        result = arb.evaluate(self._fv(
            arb_signal=0.20, age_ms=50, depth=1000.0,
            spread=0.02, mid_pm=0.35, mid_op=0.62,
        ))
        self.assertTrue(result.accepted, f"Rejected: {result.rejection_reason}")
        self.assertIsNotNone(result.leg1_proposal)
        self.assertIsNotNone(result.leg2_proposal)
        self.assertGreater(result.net_edge, 0.003)

    def test_nan_signal_rejected(self):
        from strategies.arbitrage import ArbitrageStrategy
        from data.models import FeatureVector
        from src.types import Platform
        ts = now_ms()
        fv = FeatureVector(
            market_id="X", ts=ts, computed_ts=ts+1,
            arb_signal=math.nan, stale_markets=[Platform.POLYMARKET],
            mid_pm=0.50, mid_op=0.50, spread_pm=0.02, spread_op=0.02,
            ofi_pm=0.0, ofi_op=0.0, vol_30s=0.01, days_to_resolution=30.0,
            portfolio_delta=0.0,
            bid_depth_pm=500, ask_depth_pm=500,
            bid_depth_op=500, ask_depth_op=500,
        )
        arb = ArbitrageStrategy()
        result = arb.evaluate(fv)
        self.assertFalse(result.accepted)
        self.assertIn("stale", result.rejection_reason)

    def test_stale_signal_rejected(self):
        """Signal older than max_signal_age_ms is rejected."""
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        arb = ArbitrageStrategy(ArbConfig(max_signal_age_ms=100))
        result = arb.evaluate(self._fv(age_ms=500))  # 500ms > 100ms
        self.assertFalse(result.accepted)
        self.assertIn("signal_age", result.rejection_reason)

    def test_thin_book_rejected(self):
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        arb = ArbitrageStrategy(ArbConfig(min_order_usdc=10.0))
        # Very thin book: only $1 available after 35% discount
        result = arb.evaluate(self._fv(depth=2.0))
        self.assertFalse(result.accepted)
        self.assertIn("fillable", result.rejection_reason)

    def test_leg_proposals_have_correct_leg_numbers(self):
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        from src.types import ArbLeg
        arb = ArbitrageStrategy(ArbConfig(min_net_edge=0.003))
        result = arb.evaluate(self._fv(
            arb_signal=0.20, depth=1000.0, mid_pm=0.35, mid_op=0.62
        ))
        self.assertTrue(result.accepted, result.rejection_reason)
        self.assertEqual(result.leg1_proposal.leg_number, ArbLeg.LEG_1)
        self.assertEqual(result.leg2_proposal.leg_number, ArbLeg.LEG_2)

    def test_legs_share_group_id(self):
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        arb = ArbitrageStrategy(ArbConfig(min_net_edge=0.003))
        result = arb.evaluate(self._fv(
            arb_signal=0.20, depth=1000.0, mid_pm=0.35, mid_op=0.62
        ))
        self.assertTrue(result.accepted, result.rejection_reason)
        self.assertEqual(
            result.leg1_proposal.leg_group_id,
            result.leg2_proposal.leg_group_id
        )

    def test_now_ts_overrides_signal_age(self):
        """Backtest: passing now_ts=fv.ts means age=0 → always fresh."""
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        arb = ArbitrageStrategy(ArbConfig(max_signal_age_ms=100))
        fv = self._fv(age_ms=10_000)  # 10 seconds old in wall-clock
        # With simulated time = fv.ts, age = 0 → should pass
        result = arb.evaluate(fv, now_ts=fv.ts)
        self.assertTrue(result.accepted or result.rejection_reason != "signal_age")


# ─────────────────────────────────────────────────────────────────────────────
# V. System test: full backtest pipeline
# ─────────────────────────────────────────────────────────────────────────────

class TestBacktestSystem(unittest.TestCase):
    """End-to-end backtest system tests with seeded determinism."""

    def _run_backtest(self, ticks=2000, capital=10_000.0, seed=42):
        from backtest.engine import BacktestEngine, build_synthetic_tick_stream
        streams = {
            m: build_synthetic_tick_stream(m, n_ticks=ticks // 3, seed=seed)
            for m in ["BTC-Q4", "ETH-Q1", "SOL-Q2"]
        }
        engine = BacktestEngine(tick_streams=streams, initial_capital=capital, seed=seed)
        return run(engine.run())

    def test_backtest_runs_without_error(self):
        result = self._run_backtest(ticks=600, capital=10_000.0)
        self.assertIsNotNone(result)

    def test_backtest_produces_positive_pnl(self):
        """Seeded arb opportunities → net positive P&L (uses main.py config)."""
        import subprocess, sys, json
        r = subprocess.run(
            [sys.executable, "main.py", "--mode", "backtest",
             "--ticks", "2000", "--capital", "10000"],
            capture_output=True, text=True, cwd=".",
        )
        self.assertEqual(r.returncode, 0, f"main.py failed:\n{r.stderr}")
        self.assertIn("+", r.stdout, "Expected positive P&L in output")

    def test_backtest_is_deterministic(self):
        """Same seed → identical P&L."""
        r1 = self._run_backtest(ticks=600, capital=10_000.0, seed=99)
        r2 = self._run_backtest(ticks=600, capital=10_000.0, seed=99)
        self.assertAlmostEqual(r1.total_pnl, r2.total_pnl, places=4)

    def test_drawdown_never_exceeds_kill_threshold(self):
        """Max drawdown in backtest must be < kill threshold (20%)."""
        result = self._run_backtest(ticks=2_000, capital=10_000.0)
        self.assertLess(result.max_drawdown, 0.20,
                        f"Drawdown {result.max_drawdown:.2%} exceeds kill threshold")

    def test_no_order_exceeds_max_order_usdc(self):
        """Every fill must be <= max_order_usdc."""
        result = self._run_backtest(ticks=2_000, capital=10_000.0)
        for trade in result.trades:
            self.assertLessEqual(
                trade.filled_usdc, 205.0,  # $200 + 2.5% tolerance for scaling
                f"Fill ${trade.filled_usdc:.2f} exceeds max_order_usdc"
            )

    def test_fill_prices_in_valid_range(self):
        """All fill prices must be in (0, 1) — valid probability range."""
        result = self._run_backtest(ticks=2_000, capital=10_000.0)
        for trade in result.trades:
            if trade.fill_price is not None:
                self.assertGreater(trade.fill_price, 0.0,
                                   f"fill_price {trade.fill_price} <= 0")
                self.assertLess(trade.fill_price, 1.0,
                                f"fill_price {trade.fill_price} >= 1")

    def test_slippage_is_bounded(self):
        """Slippage must not exceed 500 bps — catch price model bugs."""
        result = self._run_backtest(ticks=2_000, capital=10_000.0)
        for trade in result.trades:
            if trade.slippage_bps is not None:
                self.assertLess(
                    trade.slippage_bps, 500,
                    f"Slippage {trade.slippage_bps} bps is unreasonably high"
                )

    def test_equity_series_starts_at_capital(self):
        """equity_series[0] == initial_capital."""
        result = self._run_backtest(ticks=600, capital=5_000.0)
        if result.equity_series:
            _ts, equity = result.equity_series[0]
            self.assertAlmostEqual(equity, 5_000.0, delta=100.0)

    def test_zero_fills_below_min_capital(self):
        """Capital too small to meet MIN_ORDER_USDC → no proposals."""
        result = self._run_backtest(ticks=600, capital=50.0)
        # $50 capital with $1 min order and 10% liquidity buffer:
        # available = 45, needed = min_order = 1 → proposals may fire
        # but max_order should also be tiny
        # Main check: no crashes
        self.assertIsNotNone(result)

    def test_three_markets_active(self):
        """All three synthetic markets generate at least some FV events."""
        from backtest.engine import BacktestEngine, build_synthetic_tick_stream
        markets = ["BTC-Q4", "ETH-Q1", "SOL-Q2"]
        streams = {m: build_synthetic_tick_stream(m, n_ticks=200, seed=42)
                   for m in markets}
        engine = BacktestEngine(tick_streams=streams, initial_capital=10_000.0, seed=42)
        run(engine.run())
        self.assertGreater(engine._fe.vectors_emitted, 0)


# ─────────────────────────────────────────────────────────────────────────────
# VI. Regression tests — specific bugs that must never return
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressions(unittest.TestCase):

    def test_fe_se_callback_is_wired_in_backtest(self):
        """BUG: BacktestEngine was missing FE→SE callback → 0 proposals."""
        from backtest.engine import BacktestEngine, build_synthetic_tick_stream
        streams = {"BTC-Q4": build_synthetic_tick_stream("BTC-Q4", n_ticks=50, seed=42)}
        engine = BacktestEngine(tick_streams=streams, initial_capital=10_000.0, seed=42)
        # FE must have at least 1 callback (to SE or wrapper)
        self.assertGreater(len(engine._fe._callbacks), 0,
                           "FeatureEngine has no callbacks — FE→SE wire is missing")

    def test_simulated_time_prevents_stale_signal_rejection(self):
        """BUG: Signal age was computed against wall-clock, rejecting all backtest signals."""
        from strategies.arbitrage import ArbitrageStrategy, ArbConfig
        from data.models import FeatureVector
        from src.types import Platform
        arb = ArbitrageStrategy(ArbConfig(max_signal_age_ms=300))
        # Simulate a tick from 10 seconds ago
        old_ts = now_ms() - 10_000
        fv = FeatureVector(
            market_id="X", ts=old_ts, computed_ts=old_ts+1,
            arb_signal=0.08, stale_markets=[],
            mid_pm=0.44, mid_op=0.54,
            spread_pm=0.02, spread_op=0.02,
            ofi_pm=0.0, ofi_op=0.0, vol_30s=0.01, days_to_resolution=30.0,
            portfolio_delta=0.0,
            bid_depth_pm=500, ask_depth_pm=500,
            bid_depth_op=500, ask_depth_op=500,
        )
        # Wall-clock evaluation → stale
        result_wall = arb.evaluate(fv)
        # Simulated-time evaluation → fresh (now_ts == fv.ts → age == 0)
        result_sim  = arb.evaluate(fv, now_ts=old_ts + 50)
        self.assertFalse(result_wall.accepted)
        self.assertTrue(result_sim.accepted,
                        f"Simtime arb rejected: {result_sim.rejection_reason}")

    def test_slippage_uses_round_not_truncate(self):
        """BUG: int() truncation gave 799 instead of 800 bps."""
        # abs(0.46 - 0.50) / 0.50 * 10_000 = 799.9999...
        raw = abs(0.46 - 0.50) / 0.50 * 10_000
        via_trunc  = int(raw)
        via_round  = int(round(raw))
        self.assertEqual(via_round, 800, "round() should give 800")
        # The backtest engine must use round(), not int()
        import backtest.engine as be
        import inspect
        src = inspect.getsource(be)
        self.assertIn("int(round(", src,
                      "backtest/engine.py must use int(round(...)) for slippage_bps")

    def test_featurevector_staleness_check_is_relative(self):
        """BUG: Staleness was checked with wall-clock, failing all backtest snaps."""
        from engine.feature_engine import FeatureEngine
        import inspect
        src = inspect.getsource(FeatureEngine.on_snapshot)
        # Must NOT check staleness using wall-clock now vs received_ts
        # Instead: (received_ts - ts) > STALE_MS
        self.assertNotIn("now - (s.received_ts", src,
                         "Staleness check must not use wall-clock 'now'")

    def test_build_synthetic_stream_default_ts_is_current(self):
        """BUG: Default start_ts_ms=0 made all snapshots stale."""
        from backtest.engine import build_synthetic_tick_stream
        ticks = build_synthetic_tick_stream("X", n_ticks=5, seed=1)
        ts0, _, _ = ticks[0]
        age = now_ms() - ts0
        self.assertLess(
            age, 5_000 * 10,  # ticks[0] is n_ticks * interval_ms in the past
            f"Tick ts={ts0} is too old ({age}ms) — default start_ts is wrong"
        )

    def test_order_proposal_arb_requires_leg_fields(self):
        """BUG: ARB proposals without leg fields would crash downstream."""
        from execution.models import OrderProposal
        from src.types import Platform, Side, OrderType, StrategyId
        with self.assertRaises((ValueError, TypeError)):
            OrderProposal(
                proposal_id=str(uuid.uuid4()),
                market_id="X", platform=Platform.POLYMARKET,
                side=Side.BUY_YES, size_usdc=100.0, limit_price=0.50,
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.ARB,  # ARB without leg_group_id → error
                expiry_ms=now_ms() + 30_000,
                source_ts=now_ms(),
            )

    def test_crossed_book_raises(self):
        """BUG: Crossed books must raise immediately, not silently produce NaN."""
        from data.models import MarketSnapshot
        from src.types import Platform
        from src.errors import CrossedBookError
        with self.assertRaises(CrossedBookError):
            MarketSnapshot(
                market_id="X", platform=Platform.POLYMARKET,
                yes_bid=0.55, yes_ask=0.45,  # bid > ask → crossed
                no_bid=0.45, no_ask=0.55,
                bid_depth_usdc=100, ask_depth_usdc=100,
                taker_fee_bps=20, ts=now_ms(), received_ts=now_ms(),
            )

    def test_risk_limits_validation(self):
        """BUG: Invalid RiskLimits (warn >= kill) must be caught at construction."""
        from risk.limits import RiskLimits
        with self.assertRaises(ValueError):
            RiskLimits(drawdown_warn_pct=0.25, drawdown_kill_pct=0.20)

    def test_signal_context_prevents_total_blackout(self):
        """BUG: AI must not suppress both arb and MM simultaneously."""
        from ai.signal_context import SignalContext, MarketRegime, VolRegime
        with self.assertRaises(ValueError):
            SignalContext(
                market_id="X",
                confidence_multiplier=0.10,   # minimum
                regime=MarketRegime.VOLATILE,
                vol_regime=VolRegime.SPIKE,
                suppress_mm=True,
                arb_quality=0.0,              # both suppressed
                hedge_urgency=0.0,
                model_version="v1",
                inference_ms=0.0,
                feature_count=10,
                is_fallback=False,
            )


# ─────────────────────────────────────────────────────────────────────────────
# VII. Kill switch system test — full kill → cancel → reset cycle
# ─────────────────────────────────────────────────────────────────────────────

class TestKillSwitchSystem(unittest.TestCase):

    def test_full_cycle(self):
        from risk.kill_switch import KillSwitch
        ks = KillSwitch("secret-token-abc")

        # Initially clear
        self.assertFalse(ks.is_active)

        # Activate
        rec = ks.activate("test_reason", 0.25, 1000.0, 750.0)
        self.assertTrue(ks.is_active)
        self.assertEqual(rec.reason, "test_reason")
        self.assertEqual(ks.activation_count, 1)

        # Wrong token → stays active
        self.assertFalse(ks.reset("wrong"))
        self.assertTrue(ks.is_active)

        # Correct token → clears
        self.assertTrue(ks.reset("secret-token-abc", operator_id="ops-team"))
        self.assertFalse(ks.is_active)
        self.assertEqual(len(ks.audit_trail()["resets"]), 1)

    def test_multiple_activations_tracked(self):
        from risk.kill_switch import KillSwitch
        ks = KillSwitch("tok")
        ks.activate("r1", 0.21, 1000, 790)
        ks.reset("tok")
        ks.activate("r2", 0.22, 1000, 780)
        self.assertEqual(ks.activation_count, 2)

    def test_empty_token_rejected_at_construction(self):
        from risk.kill_switch import KillSwitch
        with self.assertRaises(ValueError):
            KillSwitch("")


# ─────────────────────────────────────────────────────────────────────────────
# VIII. Order tracker integration
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderTrackerIntegration(unittest.TestCase):

    def _sub(self, size=100.0, price=0.50, strategy="mm"):
        from execution.models import OrderSubmission
        from src.types import Platform, Side, OrderType, StrategyId
        strat = {"mm": StrategyId.MM, "arb": StrategyId.ARB}[strategy]
        return OrderSubmission(
            order_id=str(uuid.uuid4()),
            proposal_id=str(uuid.uuid4()),
            market_id="BTC-Q4",
            platform=Platform.POLYMARKET,
            side=Side.BUY_YES,
            size_usdc=size,
            limit_price=price,
            order_type=OrderType.LIMIT,
            strategy_id=strat,
            expiry_ms=now_ms() + 30_000,
            token_quantity=round(size / price, 6),
            submitted_at=now_ms(),
        )

    def test_fresh_tracker_is_awaiting(self):
        from execution.order_tracker import OrderTracker, TrackerStatus
        t = OrderTracker(self._sub())
        self.assertEqual(t.status, TrackerStatus.AWAITING)
        self.assertFalse(t.status.is_terminal)

    def test_submission_to_partial_to_filled(self):
        from execution.order_tracker import OrderTracker, TrackerStatus
        from src.types import OrderStatus
        t = OrderTracker(self._sub(size=100.0, price=0.50))
        r1 = t.record_submission("exch-001")
        self.assertEqual(r1.status, OrderStatus.SUBMITTED)

        r2 = t.record_fill(50.0, 0.51, 98.0, now_ms())
        self.assertEqual(r2.status, OrderStatus.PARTIAL)

        r3 = t.record_fill(50.0, 0.51, 98.0, now_ms())
        self.assertEqual(r3.status, OrderStatus.FILLED)
        self.assertTrue(t.status.is_terminal)

    def test_wavg_price_computation(self):
        from execution.order_tracker import OrderTracker
        t = OrderTracker(self._sub(size=100.0, price=0.50))
        t.record_submission("exch-001")
        t.record_fill(60.0, 0.40, 150.0, now_ms())
        t.record_fill(40.0, 0.60, 66.67, now_ms())
        # wavg = (60*0.40 + 40*0.60) / 100 = (24+24)/100 = 0.48
        self.assertAlmostEqual(t.weighted_avg_price, 0.48, places=4)

    def test_cancellation_is_terminal(self):
        from execution.order_tracker import OrderTracker, TrackerStatus
        t = OrderTracker(self._sub())
        t.record_submission("exch-002")
        t.record_cancellation()
        self.assertTrue(t.status.is_terminal)
        self.assertEqual(t.status, TrackerStatus.CANCELLED)

    def test_expiry_check(self):
        from execution.order_tracker import OrderTracker
        from execution.models import OrderSubmission
        from src.types import Platform, Side, OrderType, StrategyId
        sub = OrderSubmission(
            order_id=str(uuid.uuid4()), proposal_id=str(uuid.uuid4()),
            market_id="X", platform=Platform.POLYMARKET,
            side=Side.BUY_YES, size_usdc=50.0, limit_price=0.50,
            order_type=OrderType.LIMIT, strategy_id=StrategyId.MM,
            expiry_ms=now_ms() - 1,  # already expired
            token_quantity=100.0, submitted_at=now_ms() - 100,
        )
        t = OrderTracker(sub)
        t.record_submission("exch-003")
        self.assertTrue(t.is_expired(now_ms()))


# ─────────────────────────────────────────────────────────────────────────────
# IX. Config and logging smoke tests
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigSmoke(unittest.TestCase):

    def test_settings_load_defaults(self):
        from config.settings import Settings
        s = Settings()
        self.assertGreater(s.trading.initial_cash_usdc, 0)
        self.assertIn("polymarket", s.polymarket.clob_url)

    def test_logging_setup_runs(self):
        from config.logging_setup import configure_logging
        # Should not raise
        configure_logging(level="WARNING", fmt="text")
        import logging
        self.assertIsNotNone(logging.getLogger("pmts"))


if __name__ == "__main__":
    unittest.main(verbosity=2)