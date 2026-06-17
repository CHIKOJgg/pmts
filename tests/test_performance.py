"""tests/test_performance.py — Performance benchmarks for critical paths."""
from __future__ import annotations

import time

from data.models import FeatureVector, MarketSnapshot, VenueSnapshot
from execution.models import OrderProposal
from portfolio.manager import FillRecord, PortfolioManager
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits
from src.enums import OrderType, Platform, Side, StrategyId


def _make_snapshot(market_id: str, platform: Platform, ts: int) -> MarketSnapshot:
    return MarketSnapshot(
        market_id=market_id,
        platform=platform,
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.49,
        no_ask=0.51,
        bid_depth_usdc=500.0,
        ask_depth_usdc=500.0,
        taker_fee_bps=20,
        ts=ts,
        received_ts=ts,
    )


def _make_feature_vector(market_id: str, ts: int) -> FeatureVector:
    return FeatureVector(
        market_id=market_id,
        ts=ts,
        computed_ts=ts,
        arb_signal=0.01,
        stale_markets=[],
        venues={
            Platform.POLYMARKET: VenueSnapshot(mid=0.50, spread=0.02, ofi=0.1, bid_depth=500.0, ask_depth=500.0),
            Platform.OPINION: VenueSnapshot(mid=0.50, spread=0.02, ofi=0.1, bid_depth=500.0, ask_depth=500.0),
        },
        vol_30s=0.005,
        days_to_resolution=30.0,
        portfolio_delta=0.0,
    )


class TestPortfolioManagerPerformance:
    def test_10k_fills_under_1s(self) -> None:
        def price_fn(m: str, p) -> tuple:
            return (0.50, 0.50)

        pm = PortfolioManager(initial_cash_usdc=10000.0, price_source=price_fn)
        n = 10_000
        async def _run_fills():
            for i in range(n):
                fill = FillRecord(
                    proposal_id=f"p-{i}",
                    order_id=f"o-{i}",
                    market_id="test-market",
                    platform=Platform.POLYMARKET,
                    side=Side.BUY_YES.value,
                    filled_usdc=10.0,
                    fill_price=0.50,
                    ts=i * 1000,
                )
                await pm.record_fill(fill)
                
        start = time.perf_counter()
        import asyncio
        asyncio.run(_run_fills())
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"10k fills took {elapsed:.3f}s (limit: 5.0s)"
        print(f"10k fills: {elapsed:.3f}s ({n / elapsed:,.0f} ops/s)")

    def test_mtm_computation_under_100ms(self) -> None:
        def price_fn(m: str, p) -> tuple:
            return (0.50, 0.50)

        pm = PortfolioManager(initial_cash_usdc=10000.0, price_source=price_fn)
        async def _run_mtm_fills():
            for i in range(100):
                fill = FillRecord(
                    proposal_id=f"p-{i}",
                    order_id=f"o-{i}",
                    market_id=f"market-{i % 10}",
                    platform=Platform.POLYMARKET,
                    side=Side.BUY_YES.value,
                    filled_usdc=10.0,
                    fill_price=0.50,
                    ts=i * 1000,
                )
                await pm.record_fill(fill)
        import asyncio
        asyncio.run(_run_mtm_fills())

        start = time.perf_counter()
        for _ in range(1000):
            pm.get_portfolio_mtm()
        elapsed = time.perf_counter() - start
        assert elapsed < 0.1, f"1k MTM took {elapsed:.3f}s (limit: 0.1s)"
        print(f"1k MTM: {elapsed:.3f}s ({1000 / elapsed:,.0f} ops/s)")


class TestRiskEnginePerformance:
    def test_10k_evaluations_under_1s(self) -> None:
        def price_fn(m: str, p) -> tuple:
            return (0.50, 0.50)

        pm = PortfolioManager(initial_cash_usdc=10000.0, price_source=price_fn)
        ks = KillSwitch(confirmation_token="test-token-secure-123")
        limits = RiskLimits()
        risk = RiskEngine(portfolio=pm, kill_switch=ks, limits=limits)

        n = 10_000
        start = time.perf_counter()
        for i in range(n):
            proposal = OrderProposal(
                proposal_id=f"p-{i}",
                market_id="test-market",
                platform=Platform.POLYMARKET,
                side=Side.BUY_YES,
                size_usdc=10.0,
                limit_price=0.50,
                order_type=OrderType.LIMIT,
                strategy_id=StrategyId.MM,
                expiry_ms=i * 1000 + 2000,
                source_ts=i * 1000,
            )
            risk.evaluate(proposal)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"10k risk evals took {elapsed:.3f}s (limit: 5.0s)"
        print(f"10k risk evals: {elapsed:.3f}s ({n / elapsed:,.0f} ops/s)")


class TestFeatureVectorPerformance:
    def test_10k_constructions_under_1s(self) -> None:
        n = 10_000
        start = time.perf_counter()
        for i in range(n):
            _make_feature_vector("test", i * 1000)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"10k FV constructions took {elapsed:.3f}s (limit: 1.0s)"
        print(f"10k FV constructions: {elapsed:.3f}s ({n / elapsed:,.0f} ops/s)")


class TestSnapshotPerformance:
    def test_10k_constructions_under_1s(self) -> None:
        n = 10_000
        start = time.perf_counter()
        for i in range(n):
            _make_snapshot("test", Platform.POLYMARKET, i * 1000)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"10k snapshot constructions took {elapsed:.3f}s (limit: 1.0s)"
        print(f"10k snapshot constructions: {elapsed:.3f}s ({n / elapsed:,.0f} ops/s)")
