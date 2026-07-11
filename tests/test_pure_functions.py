"""Unit tests for pure, side-effect-free functions used across the pipeline.

These exercise pricing/risk math directly without any IO, network, or async.
"""

from __future__ import annotations

from execution.models import OrderProposal
from src.enums import ArbLeg, OrderType, Platform, Side, StrategyId

from risk.engine import _drawdown, _projected_delta
from strategies.arbitrage import estimate_taker_cost


def _proposal(side: Side, size_usdc: float = 100.0, limit_price: float = 0.5) -> OrderProposal:
    return OrderProposal(
        proposal_id="p1",
        market_id="M1",
        platform=Platform.POLYMARKET,
        side=side,
        size_usdc=size_usdc,
        limit_price=limit_price,
        order_type=OrderType.LIMIT,
        strategy_id=StrategyId.ARB,
        expiry_ms=0,
        source_ts=0,
        leg_group_id="g1",
        leg_number=ArbLeg.LEG_1,
        min_fill_ratio=1.0,
    )


def test_drawdown_basic() -> None:
    assert _drawdown(100.0, 90.0) == 0.10
    assert _drawdown(200.0, 150.0) == 0.25


def test_drawdown_floored_at_zero() -> None:
    assert _drawdown(100.0, 110.0) == 0.0


def test_drawdown_zero_peak() -> None:
    assert _drawdown(0.0, 90.0) == 0.0
    assert _drawdown(-5.0, 90.0) == 0.0


def test_projected_delta_buy_yes() -> None:
    # BUY_YES increases net delta by qty (= size/price)
    assert _projected_delta(0.0, _proposal(Side.BUY_YES, 100.0, 0.5)) == 200.0


def test_projected_delta_buy_no() -> None:
    assert _projected_delta(0.0, _proposal(Side.BUY_NO, 100.0, 0.5)) == -200.0


def test_projected_delta_sell_yes() -> None:
    assert _projected_delta(50.0, _proposal(Side.SELL_YES, 100.0, 0.5)) == -150.0


def test_projected_delta_sell_no() -> None:
    assert _projected_delta(50.0, _proposal(Side.SELL_NO, 100.0, 0.5)) == 250.0


def test_estimate_taker_cost_components() -> None:
    est = estimate_taker_cost(size_usdc=100.0, ask_price=0.60, bid_price=0.40, depth_usdc=1000.0, taker_fee_bps=20)
    # fee = 100 * 20/10000 = 0.20
    assert est.fee_usdc == 0.20
    # spread = 0.20, half-spread fraction = 0.20/2 / 0.60 = 0.1666...
    assert est.spread_usdc > 0.0
    assert est.impact_usdc > 0.0


def test_estimate_taker_cost_zero_spread() -> None:
    est = estimate_taker_cost(size_usdc=100.0, ask_price=0.50, bid_price=0.50, depth_usdc=1000.0, taker_fee_bps=0)
    assert est.spread_usdc == 0.0
    assert est.fee_usdc == 0.0
