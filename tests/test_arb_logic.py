"""tests/test_arb_logic.py — deterministic checks that the arbitrage strategy
captures a real cross-venue edge (independent of the fill simulator / MTM)."""

from __future__ import annotations

from data.models import FeatureVector, VenueSnapshot
from src.enums import Platform
from strategies.arbitrage import ArbConfig, ArbitrageStrategy


def _fv(pm_mid: float, op_mid: float, spread: float = 0.01, depth: float = 2000.0) -> FeatureVector:
    return FeatureVector(
        market_id="TEST",
        ts=1000,
        computed_ts=1000,
        arb_signal=1.0,
        stale_markets=[],
        venues={
            Platform.POLYMARKET: VenueSnapshot(mid=pm_mid, spread=spread, ofi=0.0, bid_depth=depth, ask_depth=depth),
            Platform.OPINION: VenueSnapshot(mid=op_mid, spread=spread, ofi=0.0, bid_depth=depth, ask_depth=depth),
        },
        vol_30s=0.001,
        days_to_resolution=5.0,
        portfolio_delta=0.0,
        vol_regime="NORMAL",
    )


def test_arb_accepts_clear_edge() -> None:
    """A genuine price gap (PM YES cheap, OP NO cheap) must be accepted with positive edge."""
    strat = ArbitrageStrategy(ArbConfig(min_net_edge=0.006))
    fv = _fv(pm_mid=0.45, op_mid=0.55)  # ~10% gross edge before costs
    res = strat.evaluate(fv, now_ts=2000)
    assert res.accepted, res.rejection_reason
    assert res.net_edge > 0
    assert res.direction == "POLYMARKET_YES_OPINION_NO"


def test_arb_rejects_when_no_edge() -> None:
    """Identical mids on both venues leave no arbitrage and must be rejected."""
    strat = ArbitrageStrategy(ArbConfig(min_net_edge=0.006))
    fv = _fv(pm_mid=0.50, op_mid=0.50)
    res = strat.evaluate(fv, now_ts=2000)
    assert not res.accepted


def test_arb_rejects_stale_signal() -> None:
    """A NaN arb_signal (stale data) must be rejected without trading."""

    from src.enums import Platform as _P

    strat = ArbitrageStrategy(ArbConfig())
    bad = _fv(0.45, 0.55).model_copy(
        update={"arb_signal": float("nan"), "stale_markets": [_P.POLYMARKET]}
    )
    res = strat.evaluate(bad, now_ts=2000)
    assert not res.accepted
