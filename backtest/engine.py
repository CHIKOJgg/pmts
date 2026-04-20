"""
backtest/engine.py — Realistic backtest engine.

Runs the EXACT same strategy/risk/portfolio stack as live trading.
Only the market data source and fill mechanism are simulated.

Realism:
  - Partial fills via Beta(2.0, 1.5) distribution — mean ≈ 57% of depth
  - 35% depth discount (displayed depth is optimistic in prediction markets)
  - Slippage computed from actual ask/bid prices, not mid
  - Per-stage normally distributed latency
  - Order expiry re-anchored to simulated timestamps
  - No instant or guaranteed fills
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data.models import MarketSnapshot
from execution.models import OrderProposal
from portfolio.manager import FillRecord, PortfolioManager
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits, DEFAULT_LIMITS
from engine.feature_engine import FeatureEngine
from engine.strategy_engine import StrategyEngine, StrategyConfig
from strategies.arbitrage import ArbConfig
from strategies.delta_neutral import DeltaNeutralConfig
from src.types import ArbLeg, OrderStatus, OrderType, Platform, Side, StrategyId

logger = logging.getLogger(__name__)

FILL_CERTAINTY:      float = 0.65
PARTIAL_FILL_ALPHA:  float = 2.0
PARTIAL_FILL_BETA:   float = 1.5
MIN_FILL_USDC:       float = 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Latency model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LatencyModel:
    tick_to_signal_mean:   float = 25.0
    tick_to_signal_std:    float = 8.0
    tick_to_signal_min:    float = 5.0
    tick_to_signal_max:    float = 150.0
    signal_to_submit_mean: float = 45.0
    signal_to_submit_std:  float = 12.0
    signal_to_submit_min:  float = 10.0
    signal_to_submit_max:  float = 400.0
    submit_to_fill_mean:   float = 70.0
    submit_to_fill_std:    float = 20.0
    submit_to_fill_min:    float = 15.0
    submit_to_fill_max:    float = 800.0

    def _sample(self, mean, std, lo, hi) -> float:
        return max(lo, min(hi, random.gauss(mean, std)))

    def tick_to_signal(self)   -> float:
        return self._sample(self.tick_to_signal_mean,   self.tick_to_signal_std,
                            self.tick_to_signal_min,   self.tick_to_signal_max)
    def signal_to_submit(self) -> float:
        return self._sample(self.signal_to_submit_mean, self.signal_to_submit_std,
                            self.signal_to_submit_min, self.signal_to_submit_max)
    def submit_to_fill(self)   -> float:
        return self._sample(self.submit_to_fill_mean,   self.submit_to_fill_std,
                            self.submit_to_fill_min,   self.submit_to_fill_max)


# ─────────────────────────────────────────────────────────────────────────────
# Simulated order
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _SimOrder:
    proposal:      OrderProposal
    submitted_at:  int   # simulated time
    expiry_ms:     int   # simulated time
    filled_usdc:   float = 0.0
    last_price:    Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# Simulated fill event
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SimFill:
    proposal_id:  str
    status:       OrderStatus
    filled_usdc:  float
    fill_price:   Optional[float]
    slippage_bps: Optional[int]
    sim_ts:       int


# ─────────────────────────────────────────────────────────────────────────────
# Fill simulator
# ─────────────────────────────────────────────────────────────────────────────

class FillSimulator:
    """
    Processes resting orders against incoming ticks.

    Core realism decisions:
      - Fill fraction drawn from Beta(2.0, 1.5): mean ≈ 57%, right-skewed
      - Displayed depth discounted by FILL_CERTAINTY (0.65)
      - Fill price is actual ask/bid (not mid) — slippage is real
      - Multiple ticks needed for large orders (no instant full fill)
    """

    def __init__(self, latency: LatencyModel) -> None:
        self._latency  = latency
        self._resting: Dict[str, _SimOrder] = {}

    def submit(self, proposal: OrderProposal, sim_ts: int) -> None:
        """Register an order in the simulated order book."""
        original_window = max(100, proposal.expiry_ms - proposal.source_ts)
        sim_expiry      = sim_ts + original_window
        self._resting[proposal.proposal_id] = _SimOrder(
            proposal=proposal,
            submitted_at=sim_ts,
            expiry_ms=sim_expiry,
        )

    def cancel(self, proposal_id: str) -> Optional[_SimOrder]:
        return self._resting.pop(proposal_id, None)

    def process_tick(
        self, snap: MarketSnapshot, sim_ts: int
    ) -> List[SimFill]:
        """Check all resting orders against this snapshot."""
        events:    List[SimFill] = []
        to_remove: List[str]     = []

        for pid, order in list(self._resting.items()):
            # Expiry check
            if sim_ts >= order.expiry_ms:
                to_remove.append(pid)
                events.append(SimFill(
                    proposal_id=pid,
                    status=OrderStatus.CANCELLED,
                    filled_usdc=order.filled_usdc,
                    fill_price=order.last_price,
                    slippage_bps=None,
                    sim_ts=sim_ts,
                ))
                continue

            # Market/platform filter
            if (snap.market_id != order.proposal.market_id
                    or snap.platform != order.proposal.platform):
                continue

            evt = self._try_fill(order, snap, sim_ts)
            if evt is None:
                continue

            events.append(evt)
            if evt.status in (OrderStatus.FILLED, OrderStatus.REJECTED):
                to_remove.append(pid)
            elif evt.status == OrderStatus.PARTIAL:
                order.filled_usdc += evt.filled_usdc
                order.last_price   = evt.fill_price

        for pid in to_remove:
            self._resting.pop(pid, None)
        return events

    def _try_fill(
        self, order: _SimOrder, snap: MarketSnapshot, sim_ts: int
    ) -> Optional[SimFill]:
        side      = order.proposal.side
        limit     = order.proposal.limit_price
        remaining = order.proposal.size_usdc - order.filled_usdc

        if remaining < MIN_FILL_USDC:
            return None

        # Determine crossing condition using ACTUAL prices (not mid)
        if side == Side.BUY_YES:
            market_price = snap.yes_ask
            depth        = snap.ask_depth_usdc
            crosses      = limit >= market_price
        elif side == Side.BUY_NO:
            market_price = snap.no_ask
            depth        = snap.ask_depth_usdc
            crosses      = limit >= market_price
        elif side == Side.SELL_YES:
            market_price = snap.yes_bid
            depth        = snap.bid_depth_usdc
            crosses      = limit <= market_price
        else:  # SELL_NO
            market_price = snap.no_bid
            depth        = snap.bid_depth_usdc
            crosses      = limit <= market_price

        if not crosses:
            return None

        # Stochastic fill fraction
        frac       = random.betavariate(PARTIAL_FILL_ALPHA, PARTIAL_FILL_BETA)
        available  = depth * FILL_CERTAINTY * frac
        fill_usdc  = min(remaining, available)

        if fill_usdc < MIN_FILL_USDC:
            return None

        # Slippage: difference between fill price and limit
        slippage_bps = None
        if limit > 0:
            slippage_bps = max(0, int(round(abs(market_price - limit) / limit * 10_000)))

        is_full = (remaining - fill_usdc) < MIN_FILL_USDC
        status  = OrderStatus.FILLED if is_full else OrderStatus.PARTIAL

        return SimFill(
            proposal_id=order.proposal.proposal_id,
            status=status,
            filled_usdc=fill_usdc,
            fill_price=market_price,
            slippage_bps=slippage_bps,
            sim_ts=sim_ts,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Trade record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    proposal_id:    str
    market_id:      str
    platform:       str
    strategy_id:    str
    side:           str
    requested_usdc: float
    limit_price:    float
    filled_usdc:    float
    fill_price:     Optional[float]
    fill_ratio:     float
    slippage_bps:   Optional[int]
    submitted_ts:   int
    fill_ts:        Optional[int]
    latency_ms:     Optional[int]
    risk_verdict:   str
    reject_reason:  Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# Backtest result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    start_ts:        int
    end_ts:          int
    initial_capital: float
    market_ids:      List[str]
    total_ticks:     int
    final_equity:    float
    total_return:    float
    total_pnl:       float
    realised_pnl:    float
    unrealised_pnl:  float
    max_drawdown:    float
    sharpe_ratio:    Optional[float]
    sortino_ratio:   Optional[float]
    total_proposals: int
    approved_count:  int
    rejected_count:  int
    filled_count:    int
    partial_count:   int
    expired_count:   int
    avg_fill_ratio:  float
    avg_slippage_bps: Optional[float]
    fill_rate:       float
    trades:          List[TradeRecord]
    equity_series:   List[Tuple[int, float]]

    @property
    def duration_days(self) -> float:
        return (self.end_ts - self.start_ts) / 86_400_000

    def summary(self) -> str:
        sr = f"{self.sharpe_ratio:.2f}" if self.sharpe_ratio is not None else "N/A"
        so = f"{self.sortino_ratio:.2f}" if self.sortino_ratio is not None else "N/A"
        sl = f"{self.avg_slippage_bps:.1f}" if self.avg_slippage_bps is not None else "N/A"
        return "\n".join([
            "═══ BACKTEST RESULTS ═══",
            f"Duration:     {self.duration_days:.1f} days  |  {self.total_ticks} ticks",
            f"Markets:      {', '.join(self.market_ids)}",
            f"P&L:          ${self.total_pnl:+.2f}  ({self.total_return:+.2%})",
            f"Max Drawdown: {self.max_drawdown:.2%}",
            f"Sharpe:       {sr}  |  Sortino: {so}",
            "",
            f"Proposals:    {self.total_proposals} eval  |  {self.approved_count} approved  |  {self.rejected_count} rejected",
            f"Fills:        {self.filled_count} full  |  {self.partial_count} partial  |  {self.expired_count} expired",
            f"Fill rate:    {self.fill_rate:.1%}  |  Avg fill ratio: {self.avg_fill_ratio:.1%}",
            f"Avg slippage: {sl} bps",
        ])


# ─────────────────────────────────────────────────────────────────────────────
# BacktestEngine
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Drives the full trading stack with simulated market data.

    Runs the same FeatureEngine, StrategyEngine, RiskEngine, and PortfolioManager
    as live trading. Only the data source and fill mechanism are simulated.
    """

    def __init__(
        self,
        tick_streams:    Dict[str, List[Tuple[int, MarketSnapshot, MarketSnapshot]]],
        initial_capital: float             = 10_000.0,
        latency_model:   LatencyModel      = None,
        strategy_config: StrategyConfig    = None,
        arb_config:      ArbConfig         = None,
        dn_config:       DeltaNeutralConfig = None,
        risk_limits:     RiskLimits        = None,
        kill_token:      str               = "backtest-kill-token",
        seed:            Optional[int]     = None,
    ) -> None:
        if seed is not None:
            random.seed(seed)

        self._streams   = tick_streams
        self._latency   = latency_model or LatencyModel()
        self._initial   = initial_capital

        # Latest snapshots for the price_source callback
        self._latest: Dict[Tuple[str, Platform], MarketSnapshot] = {}

        def price_source(mid: str, plat: Platform):
            s = self._latest.get((mid, plat))
            return (s.yes_mid, s.no_mid) if s else None

        self._portfolio   = PortfolioManager(initial_capital, price_source)
        self._kill_switch = KillSwitch(kill_token)
        self._risk        = RiskEngine(
            portfolio=self._portfolio,
            kill_switch=self._kill_switch,
            limits=risk_limits or DEFAULT_LIMITS,
        )
        self._fe  = FeatureEngine(self._portfolio)
        self._se  = StrategyEngine(
            config=strategy_config or StrategyConfig(),
            arb_config=arb_config or ArbConfig(),
            dn_config=dn_config or DeltaNeutralConfig(),
        )
        # Wire FeatureEngine → StrategyEngine so FV callbacks reach the strategies
        self._fe.add_callback(self._se.on_feature_vector)
        self._sim = FillSimulator(self._latency)

        # pending: proposal_id → (proposal, submit_ts)
        self._pending: Dict[str, Tuple[OrderProposal, int]] = {}
        self._trades:  List[TradeRecord] = []
        self._equity:  List[Tuple[int, float]] = []
        self._approved = 0
        self._rejected = 0
        self._total    = 0

    async def run(self) -> BacktestResult:
        """Execute the full backtest."""
        await self._portfolio.start()

        tick_proposals: List[OrderProposal] = []

        async def _collect(p: OrderProposal) -> None:
            tick_proposals.append(p)

        self._se.add_proposal_callback(_collect)

        # Wrapper: intercept FE→SE to pass simulated now_ts (signal age fix)
        # Remove the direct FE→SE callback added in __init__ and replace with
        # a closure that captures the current tick's submit_ts.
        if self._se.on_feature_vector in self._fe._callbacks:
            self._fe._callbacks.remove(self._se.on_feature_vector)

        _current_submit_ts: list = [0]  # mutable cell for closure capture

        async def _fe_to_se_with_simtime(fv: "FeatureVector") -> None:
            await self._se.on_feature_vector(fv, now_ts=_current_submit_ts[0])

        self._fe.add_callback(_fe_to_se_with_simtime)

        # Merge all tick streams in chronological order
        all_ticks: List[Tuple[int, str, MarketSnapshot, MarketSnapshot]] = []
        for mid, ticks in self._streams.items():
            for ts, pm, op in ticks:
                all_ticks.append((ts, mid, pm, op))
        all_ticks.sort(key=lambda x: x[0])

        start_ts    = all_ticks[0][0]  if all_ticks else 0
        end_ts      = all_ticks[-1][0] if all_ticks else 0
        total_ticks = 0

        for ts, market_id, snap_pm, snap_op in all_ticks:
            total_ticks += 1

            # Update price source
            self._latest[(market_id, Platform.POLYMARKET)] = snap_pm
            self._latest[(market_id, Platform.OPINION)]    = snap_op

            # Step 1+2: Feature computation (with simulated latency)
            signal_ts  = ts + int(self._latency.tick_to_signal())
            submit_ts  = signal_ts + int(self._latency.signal_to_submit())

            tick_proposals.clear()
            _current_submit_ts[0] = submit_ts   # simulated time for signal-age check
            await self._fe.on_snapshot(snap_pm)
            await self._fe.on_snapshot(snap_op)
            # StrategyEngine callbacks populated tick_proposals synchronously

            # Step 3+4: Risk gate and submit to simulator
            for proposal in list(tick_proposals):
                self._total += 1
                decision = self._risk.evaluate(proposal)

                if decision.rejected:
                    self._rejected += 1
                    self._trades.append(TradeRecord(
                        proposal_id=proposal.proposal_id,
                        market_id=proposal.market_id,
                        platform=proposal.platform.value,
                        strategy_id=proposal.strategy_id.value,
                        side=proposal.side.value,
                        requested_usdc=proposal.size_usdc,
                        limit_price=proposal.limit_price,
                        filled_usdc=0.0,
                        fill_price=None,
                        fill_ratio=0.0,
                        slippage_bps=None,
                        submitted_ts=submit_ts,
                        fill_ts=None,
                        latency_ms=None,
                        risk_verdict=decision.verdict.value,
                        reject_reason=(
                            decision.reject_reason.value
                            if decision.reject_reason else None
                        ),
                    ))
                    continue

                self._approved += 1
                self._pending[proposal.proposal_id] = (proposal, submit_ts)
                self._sim.submit(proposal, submit_ts)

            # Step 5: Process fills
            fill_ts = submit_ts + int(self._latency.submit_to_fill())
            for snap in [snap_pm, snap_op]:
                for evt in self._sim.process_tick(snap, fill_ts):
                    await self._handle_fill(evt)

            # Equity snapshot every 100 ticks
            if total_ticks % 100 == 0:
                mtm = self._portfolio.get_portfolio_mtm()
                self._equity.append((ts, mtm.total_equity_usdc))

        # Final equity point
        mtm = self._portfolio.get_portfolio_mtm()
        self._equity.append((end_ts, mtm.total_equity_usdc))

        await self._portfolio.stop()
        return self._build_result(start_ts, end_ts, total_ticks, list(self._streams.keys()))

    async def _handle_fill(self, evt: SimFill) -> None:
        pending = self._pending.get(evt.proposal_id)
        if pending is None:
            return

        proposal, submit_ts = pending

        # Record fill in portfolio
        if evt.filled_usdc > 0 and evt.fill_price is not None:
            fill = FillRecord(
                proposal_id=evt.proposal_id,
                order_id=str(uuid.uuid4()),
                market_id=proposal.market_id,
                platform=proposal.platform,
                side=proposal.side.value,
                filled_usdc=evt.filled_usdc,
                fill_price=evt.fill_price,
                ts=evt.sim_ts,
            )
            await self._portfolio.record_fill(fill)

            # Release strategy budget
            if proposal.strategy_id == StrategyId.ARB:
                self._se.notify_arb_terminal(evt.filled_usdc)
            else:
                self._se.notify_mm_terminal(evt.filled_usdc)

        # Release risk reservation
        await self._risk.notify_terminal(
            evt.proposal_id, proposal.platform, proposal.size_usdc
        )

        # Record trade
        fill_ratio = (
            min(1.0, evt.filled_usdc / proposal.size_usdc)
            if proposal.size_usdc > 0 else 0.0
        )
        latency_ms = evt.sim_ts - submit_ts if evt.sim_ts > submit_ts else None

        self._trades.append(TradeRecord(
            proposal_id=evt.proposal_id,
            market_id=proposal.market_id,
            platform=proposal.platform.value,
            strategy_id=proposal.strategy_id.value,
            side=proposal.side.value,
            requested_usdc=proposal.size_usdc,
            limit_price=proposal.limit_price,
            filled_usdc=evt.filled_usdc,
            fill_price=evt.fill_price,
            fill_ratio=fill_ratio,
            slippage_bps=evt.slippage_bps,
            submitted_ts=submit_ts,
            fill_ts=evt.sim_ts,
            latency_ms=latency_ms,
            risk_verdict="approved",
            reject_reason=None,
        ))

        if evt.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._pending.pop(evt.proposal_id, None)

    def _build_result(
        self,
        start_ts:    int,
        end_ts:      int,
        total_ticks: int,
        market_ids:  List[str],
    ) -> BacktestResult:
        mtm       = self._portfolio.get_portfolio_mtm()
        final_eq  = mtm.total_equity_usdc
        total_pnl = final_eq - self._initial
        total_ret = total_pnl / self._initial if self._initial > 0 else 0.0

        snap         = self._portfolio.build_snapshot()
        realised_pnl = snap["total_realised_pnl"]
        unrealised   = total_pnl - realised_pnl

        approved  = [t for t in self._trades if t.risk_verdict == "approved"]
        filled    = [t for t in approved if t.fill_ratio >= 0.999]
        partial   = [t for t in approved if 0 < t.fill_ratio < 0.999]
        expired   = [t for t in approved if t.fill_ratio == 0.0 and t.fill_ts is not None]

        avg_fr = (
            statistics.mean(t.fill_ratio for t in approved)
            if approved else 0.0
        )
        slips  = [t.slippage_bps for t in approved if t.slippage_bps is not None]
        avg_sl = statistics.mean(slips) if slips else None
        fr     = (len(filled) + len(partial)) / max(1, len(approved))

        return BacktestResult(
            start_ts=start_ts,
            end_ts=end_ts,
            initial_capital=self._initial,
            market_ids=market_ids,
            total_ticks=total_ticks,
            final_equity=final_eq,
            total_return=total_ret,
            total_pnl=total_pnl,
            realised_pnl=realised_pnl,
            unrealised_pnl=unrealised,
            max_drawdown=_max_drawdown(self._equity),
            sharpe_ratio=_sharpe(self._equity),
            sortino_ratio=_sortino(self._equity),
            total_proposals=self._total,
            approved_count=self._approved,
            rejected_count=self._rejected,
            filled_count=len(filled),
            partial_count=len(partial),
            expired_count=len(expired),
            avg_fill_ratio=avg_fr,
            avg_slippage_bps=avg_sl,
            fill_rate=fr,
            trades=self._trades,
            equity_series=self._equity,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic data generator
# ─────────────────────────────────────────────────────────────────────────────

def build_synthetic_tick_stream(
    market_id:        str,
    n_ticks:          int           = 1_000,
    start_ts_ms:      int           = -1,  # -1 = use current wall-clock time
    tick_interval_ms: int           = 500,
    initial_pm_mid:   float         = 0.45,
    initial_op_mid:   float         = 0.55,
    vol:              float         = 0.005,
    spread:           float         = 0.012,
    pm_fee_bps:       int           = 20,
    op_fee_bps:       int           = 25,
    seed:             Optional[int] = None,
) -> List[Tuple[int, MarketSnapshot, MarketSnapshot]]:
    """
    Generate a correlated random-walk tick stream for backtesting.

    - Both venues share a common price shock (70% correlated)
    - Mean-reversion toward 0.5
    - Depth varies randomly each tick
    - days_to_resolution decreases linearly from 10 to 0
    """
    if seed is not None:
        random.seed(seed)

    # Resolve sentinel: use wall-clock so snapshots are never stale
    if start_ts_ms < 0:
        import time as _time
        start_ts_ms = int(_time.time() * 1000) - n_ticks * tick_interval_ms

    ticks: List[Tuple[int, MarketSnapshot, MarketSnapshot]] = []
    pm, op = initial_pm_mid, initial_op_mid
    ts     = start_ts_ms

    for i in range(n_ticks):
        # Correlated mean-reverting random walk
        rev_pm = 0.01 * (0.5 - pm)
        rev_op = 0.01 * (0.5 - op)
        common = random.gauss(0, vol)
        pm     = max(0.02, min(0.98, pm + 0.7*common + 0.3*random.gauss(0,vol) + rev_pm))
        op     = max(0.02, min(0.98, op + 0.7*common + 0.3*random.gauss(0,vol) + rev_op))

        d_pm = random.uniform(200, 2000)
        d_op = random.uniform(200, 2000)
        days = max(0.1, 10.0 - i * 10.0 / n_ticks)

        def _s(mid, depth, plat, fee):
            no_mid = 1.0 - mid
            return MarketSnapshot(
                market_id=market_id,
                platform=plat,
                yes_bid=max(0.01, mid - spread / 2),
                yes_ask=min(0.99, mid + spread / 2),
                no_bid=max(0.01, no_mid - spread / 2),
                no_ask=min(0.99, no_mid + spread / 2),
                bid_depth_usdc=depth,
                ask_depth_usdc=depth * 0.9,
                taker_fee_bps=fee,
                ts=ts,
                received_ts=ts + random.randint(1, 8),
                days_to_resolution=days,
            )

        ticks.append((
            ts,
            _s(pm, d_pm, Platform.POLYMARKET, pm_fee_bps),
            _s(op, d_op, Platform.OPINION,    op_fee_bps),
        ))
        ts += tick_interval_ms

    return ticks


# ─────────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _returns(series: List[Tuple[int, float]]) -> List[float]:
    return [
        (series[i][1] - series[i-1][1]) / series[i-1][1]
        for i in range(1, len(series))
        if series[i-1][1] > 0
    ]


def _max_drawdown(series: List[Tuple[int, float]]) -> float:
    if not series:
        return 0.0
    peak, max_dd = series[0][1], 0.0
    for _, eq in series:
        if eq > peak:
            peak = eq
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return max_dd


def _periods_per_year(series: List[Tuple[int, float]]) -> float:
    if len(series) < 2:
        return 252.0
    span_ms  = series[-1][0] - series[0][0]
    n        = len(series)
    ms_per_y = 365.25 * 24 * 3600 * 1000
    return max(1.0, n / span_ms * ms_per_y) if span_ms > 0 else 252.0


def _sharpe(
    series: List[Tuple[int, float]], rfr: float = 0.0
) -> Optional[float]:
    r = _returns(series)
    if len(r) < 10:
        return None
    try:
        m, s = statistics.mean(r), statistics.stdev(r)
        if s == 0:
            return None
        n = _periods_per_year(series)
        return (m - rfr / n) / s * math.sqrt(n)
    except statistics.StatisticsError:
        return None


def _sortino(
    series: List[Tuple[int, float]], rfr: float = 0.0
) -> Optional[float]:
    r = _returns(series)
    if len(r) < 10:
        return None
    down = [x for x in r if x < 0]
    if len(down) < 2:
        return None
    try:
        m  = statistics.mean(r)
        ds = statistics.stdev(down)
        if ds == 0:
            return None
        n = _periods_per_year(series)
        return (m - rfr / n) / ds * math.sqrt(n)
    except statistics.StatisticsError:
        return None