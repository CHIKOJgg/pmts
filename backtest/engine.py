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

import logging
import math
import random
import statistics
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from data.models import FeatureVector, MarketSnapshot
from engine.feature_engine import FeatureEngine
from engine.strategy_engine import StrategyConfig, StrategyEngine
from execution.models import OrderProposal
from portfolio.manager import FillRecord, PortfolioManager
from risk.engine import RiskEngine, _drawdown
from risk.kill_switch import KillSwitch
from risk.limits import DEFAULT_LIMITS, RiskLimits
from src.clock import SimClock
from src.enums import OrderStatus, Platform, Side, StrategyId
from src.errors import NegativeHoldings
from strategies.arbitrage import ArbConfig
from strategies.delta_neutral import DeltaNeutralConfig

logger = logging.getLogger(__name__)

FILL_CERTAINTY: float = 0.50  # prediction market books are thinner than displayed
PARTIAL_FILL_ALPHA: float = 1.5
PARTIAL_FILL_BETA: float = 2.0  # mean ~43%, right-skewed but pessimistic for thin books
MIN_FILL_USDC: float = 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Latency model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LatencyModel:
    tick_to_signal_mean: float = 25.0
    tick_to_signal_std: float = 8.0
    tick_to_signal_min: float = 5.0
    tick_to_signal_max: float = 150.0
    signal_to_submit_mean: float = 45.0
    signal_to_submit_std: float = 12.0
    signal_to_submit_min: float = 10.0
    signal_to_submit_max: float = 400.0
    submit_to_fill_mean: float = 70.0
    submit_to_fill_std: float = 20.0
    submit_to_fill_min: float = 15.0
    submit_to_fill_max: float = 800.0

    def _sample(self, mean: float, std: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, random.gauss(mean, std)))

    def tick_to_signal(self) -> float:
        return self._sample(
            self.tick_to_signal_mean, self.tick_to_signal_std, self.tick_to_signal_min, self.tick_to_signal_max
        )

    def signal_to_submit(self) -> float:
        return self._sample(
            self.signal_to_submit_mean, self.signal_to_submit_std, self.signal_to_submit_min, self.signal_to_submit_max
        )

    def submit_to_fill(self) -> float:
        return self._sample(
            self.submit_to_fill_mean, self.submit_to_fill_std, self.submit_to_fill_min, self.submit_to_fill_max
        )


# ─────────────────────────────────────────────────────────────────────────────
# Simulated order
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _SimOrder:
    proposal: OrderProposal
    submitted_at: int  # simulated time
    expiry_ms: int  # simulated time
    filled_usdc: float = 0.0
    last_price: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# Simulated fill event
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SimFill:
    proposal_id: str
    status: OrderStatus
    filled_usdc: float
    fill_price: Optional[float]
    slippage_bps: Optional[int]
    sim_ts: int


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
        self._latency = latency
        self._resting: Dict[str, _SimOrder] = {}

    def submit(self, proposal: OrderProposal, sim_ts: int) -> None:
        """Register an order in the simulated order book."""
        original_window = max(100, proposal.expiry_ms - proposal.source_ts)
        sim_expiry = sim_ts + original_window
        self._resting[proposal.proposal_id] = _SimOrder(
            proposal=proposal,
            submitted_at=sim_ts,
            expiry_ms=sim_expiry,
        )

    def cancel(self, proposal_id: str) -> Optional[_SimOrder]:
        return self._resting.pop(proposal_id, None)

    def process_tick(self, snap: MarketSnapshot, sim_ts: int) -> List[SimFill]:
        """Check all resting orders against this snapshot."""
        events: List[SimFill] = []
        to_remove: List[str] = []

        for pid, order in list(self._resting.items()):
            # Expiry check
            if sim_ts >= order.expiry_ms:
                to_remove.append(pid)
                events.append(
                    SimFill(
                        proposal_id=pid,
                        status=OrderStatus.CANCELLED,
                        filled_usdc=order.filled_usdc,
                        fill_price=order.last_price,
                        slippage_bps=None,
                        sim_ts=sim_ts,
                    )
                )
                continue

            # Market/platform filter
            if snap.market_id != order.proposal.market_id or snap.platform != order.proposal.platform:
                continue

            evt = self._try_fill(order, snap, sim_ts)
            if evt is None:
                continue

            events.append(evt)
            if evt.status in (OrderStatus.FILLED, OrderStatus.REJECTED):
                to_remove.append(pid)
            elif evt.status == OrderStatus.PARTIAL:
                order.filled_usdc += evt.filled_usdc
                order.last_price = evt.fill_price

        for pid in to_remove:
            self._resting.pop(pid, None)
        return events

    def _try_fill(self, order: _SimOrder, snap: MarketSnapshot, sim_ts: int) -> Optional[SimFill]:
        side = order.proposal.side
        limit = order.proposal.limit_price
        remaining = order.proposal.size_usdc - order.filled_usdc

        if remaining < MIN_FILL_USDC:
            return None

        # Determine crossing condition using ACTUAL prices (not mid)
        if side == Side.BUY_YES:
            market_price = snap.yes_ask
            depth = snap.ask_depth_usdc
            crosses = limit >= market_price
        elif side == Side.BUY_NO:
            market_price = snap.no_ask
            depth = snap.ask_depth_usdc
            crosses = limit >= market_price
        elif side == Side.SELL_YES:
            market_price = snap.yes_bid
            depth = snap.bid_depth_usdc
            crosses = limit <= market_price
        else:  # SELL_NO
            market_price = snap.no_bid
            depth = snap.bid_depth_usdc
            crosses = limit <= market_price

        if not crosses:
            return None

        # Slippage model based on order size relative to depth
        fill_ratio = min(1.0, remaining / max(depth, 1.0))

        # Larger orders get worse slippage (sqrt impact model)
        impact_factor = 0.012  # calibrated to thin books
        impact_bps = int(round(impact_factor * math.sqrt(fill_ratio) * 10_000))

        # Adverse selection: 30% chance of informed flow picking off resting orders
        # Adds 5-25 bps penalty to simulate adverse price movement after fill
        adverse_bps = 0
        if random.random() < 0.30:
            adverse_bps = random.randint(5, 25)

        # Apply slippage and adverse selection to market price
        total_bps = impact_bps + adverse_bps
        if side.is_buy:
            effective_price = market_price * (1 + total_bps / 10_000)
        else:
            effective_price = market_price * (1 - total_bps / 10_000)

        # Stochastic fill fraction with depth consideration
        frac = random.betavariate(PARTIAL_FILL_ALPHA, PARTIAL_FILL_BETA)
        available = depth * FILL_CERTAINTY * frac
        fill_usdc = min(remaining, available)

        if fill_usdc < MIN_FILL_USDC:
            return None

        is_full = (remaining - fill_usdc) < MIN_FILL_USDC
        status = OrderStatus.FILLED if is_full else OrderStatus.PARTIAL

        # Use effective price for slippage calculation against limit
        actual_slippage_bps = None
        if limit > 0:
            actual_slippage_bps = int(round(abs(effective_price - limit) / limit * 10_000))

        return SimFill(
            proposal_id=order.proposal.proposal_id,
            status=status,
            filled_usdc=fill_usdc,
            fill_price=effective_price,  # Use effective price with slippage
            slippage_bps=actual_slippage_bps,
            sim_ts=sim_ts,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Trade record
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    proposal_id: str
    market_id: str
    platform: str
    strategy_id: str
    side: str
    requested_usdc: float
    limit_price: float
    filled_usdc: float
    fill_price: Optional[float]
    fill_ratio: float
    slippage_bps: Optional[int]
    submitted_ts: int
    fill_ts: Optional[int]
    latency_ms: Optional[int]
    risk_verdict: str
    reject_reason: Optional[str]


# ─────────────────────────────────────────────────────────────────────────────
# Backtest result
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class BacktestResult:
    start_ts: int
    end_ts: int
    initial_capital: float
    market_ids: List[str]
    total_ticks: int
    final_equity: float
    total_return: float
    total_pnl: float
    realised_pnl: float
    unrealised_pnl: float
    max_drawdown: float
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    total_proposals: int
    approved_count: int
    rejected_count: int
    filled_count: int
    partial_count: int
    expired_count: int
    avg_fill_ratio: float
    avg_slippage_bps: Optional[float]
    fill_rate: float
    trades: List[TradeRecord]
    equity_series: List[Tuple[int, float]]
    reject_reasons: Optional[Dict[str, int]] = None
    no_trade_expected: bool = False

    @property
    def duration_days(self) -> float:
        return (self.end_ts - self.start_ts) / 86_400_000

    def summary(self) -> str:
        sr = f"{self.sharpe_ratio:.2f}" if self.sharpe_ratio is not None else "N/A"
        so = f"{self.sortino_ratio:.2f}" if self.sortino_ratio is not None else "N/A"
        sl = f"{self.avg_slippage_bps:.1f}" if self.avg_slippage_bps is not None else "N/A"
        lines = [
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
        ]
        if self.reject_reasons:
            reasons = sorted(self.reject_reasons.items(), key=lambda x: -x[1])
            lines.append(f"Top reject:   {reasons[0][0]} ({reasons[0][1]})")
            if len(reasons) > 1:
                for r, c in reasons[1:]:
                    lines.append(f"              {r} ({c})")
        if self.total_proposals == 0 or (self.filled_count + self.partial_count) == 0:
            verdict = "expected" if self.no_trade_expected else "UNEXPECTED"
            lines.append(f"No-trade:     {verdict}")
        return "\n".join(lines)


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
        tick_streams: Dict[str, List[Tuple[int, MarketSnapshot, MarketSnapshot]]],
        initial_capital: float = 10_000.0,
        latency_model: Optional[LatencyModel] = None,
        strategy_config: Optional[StrategyConfig] = None,
        arb_config: Optional[ArbConfig] = None,
        dn_config: Optional[DeltaNeutralConfig] = None,
        risk_limits: Optional[RiskLimits] = None,
        kill_token: str = "backtest-kill-token",
        seed: Optional[int] = None,
    ) -> None:
        if seed is not None:
            random.seed(seed)

        self._streams = tick_streams
        self._latency = latency_model or LatencyModel()
        self._initial = initial_capital

        # Latest snapshots for the price_source callback
        self._latest: Dict[Tuple[str, Platform], MarketSnapshot] = {}

        def price_source(mid: str, plat: Platform) -> Optional[Tuple[float, float]]:
            s = self._latest.get((mid, plat))
            return (s.yes_mid, s.no_mid) if s else None

        self._sim_clock = SimClock()
        self._portfolio = PortfolioManager(initial_capital, price_source, clock=self._sim_clock)
        self._kill_switch = KillSwitch(kill_token)
        self._risk = RiskEngine(
            portfolio=self._portfolio,
            kill_switch=self._kill_switch,
            limits=risk_limits or DEFAULT_LIMITS,
            clock=self._sim_clock,
        )
        self._fe = FeatureEngine(self._portfolio, clock=self._sim_clock)
        self._se = StrategyEngine(
            config=strategy_config or StrategyConfig(),
            arb_config=arb_config or ArbConfig(),
            dn_config=dn_config or DeltaNeutralConfig(),
        )
        # Wire FeatureEngine → StrategyEngine so FV callbacks reach the strategies
        self._fe.add_callback(self._se.on_feature_vector)
        self._sim = FillSimulator(self._latency)

        # Kill switch check interval (every 50 ticks or when equity changes significantly)
        self._kill_switch_check_interval = 50

        # pending: proposal_id → (proposal, submit_ts)
        self._pending: Dict[str, Tuple[OrderProposal, int]] = {}
        self._trades: List[TradeRecord] = []
        self._equity: List[Tuple[int, float]] = []
        self._approved = 0
        self._rejected = 0
        self._total = 0
        self._reject_reasons: Dict[str, int] = {}
        # Track completed arb groups for _finalize_arb (Bug #32)
        self._arb_completed_groups: set[tuple[str, str]] = set()

    async def run(self) -> BacktestResult:
        """Execute the full backtest."""
        await self._portfolio.start()

        tick_proposals: List[OrderProposal] = []

        async def _collect(p: OrderProposal) -> None:
            tick_proposals.append(p)

        self._se.add_proposal_callback(_collect)

        # Wrapper: intercept FE→SE to pass simulated now_ts (signal age fix)
        # Replace the direct FE→SE callback added in __init__ with a closure
        # that captures the current tick's submit_ts.
        _current_submit_ts: list[int] = [0]  # mutable cell for closure capture

        async def _fe_to_se_with_simtime(fv: "FeatureVector") -> None:
            await self._se.on_feature_vector(fv, now_ts=_current_submit_ts[0])

        self._fe.replace_callback(self._se.on_feature_vector, _fe_to_se_with_simtime)

        # Merge all tick streams in chronological order
        all_ticks: List[Tuple[int, str, MarketSnapshot, MarketSnapshot]] = []
        for mid, ticks in self._streams.items():
            for ts, pm, op in ticks:
                all_ticks.append((ts, mid, pm, op))
        all_ticks.sort(key=lambda x: x[0])

        start_ts = all_ticks[0][0] if all_ticks else 0
        end_ts = all_ticks[-1][0] if all_ticks else 0
        total_ticks = 0
        prev_ts = start_ts

        # Initialize simulated clock to first tick's timestamp
        self._sim_clock.advance_to(start_ts)

        # Equity snapshot at t=0 so the series starts at initial capital
        # (periodic samples below begin after the first 100 ticks of trading).
        mtm = self._portfolio.get_portfolio_mtm()
        self._equity.append((start_ts, mtm.total_equity_usdc))

        for ts, market_id, snap_pm, snap_op in all_ticks:
            total_ticks += 1

            # Advance simulated clock to this tick's timestamp
            self._sim_clock.advance(ts - prev_ts)
            prev_ts = ts

            # Update price source
            self._latest[(market_id, Platform.POLYMARKET)] = snap_pm
            self._latest[(market_id, Platform.OPINION)] = snap_op

            # Step 1+2: Feature computation (with simulated latency)
            signal_ts = ts + int(self._latency.tick_to_signal())
            submit_ts = signal_ts + int(self._latency.signal_to_submit())

            tick_proposals.clear()
            _current_submit_ts[0] = submit_ts  # simulated time for signal-age check
            await self._fe.on_snapshot(snap_pm)
            await self._fe.on_snapshot(snap_op)
            # StrategyEngine callbacks populated tick_proposals synchronously

            # Step 3+4: Risk gate and submit to simulator
            for proposal in list(tick_proposals):
                self._total += 1
                decision = self._risk.evaluate(proposal)

                if decision.rejected:
                    self._rejected += 1
                    rej = decision.reject_reason.value if decision.reject_reason else "unknown"
                    self._reject_reasons[rej] = self._reject_reasons.get(rej, 0) + 1
                    self._trades.append(
                        TradeRecord(
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
                            reject_reason=(decision.reject_reason.value if decision.reject_reason else None),
                        )
                    )
                    continue

                self._approved += 1
                self._pending[proposal.proposal_id] = (proposal, submit_ts)
                self._sim.submit(proposal, submit_ts)

            # Step 5: Process fills (buys before sells to prevent NegativeHoldings)
            fill_ts = submit_ts + int(self._latency.submit_to_fill())
            all_events: List[SimFill] = []
            for snap in [snap_pm, snap_op]:
                all_events.extend(self._sim.process_tick(snap, fill_ts))

            def _buy_key(e: SimFill) -> int:
                p = self._pending.get(e.proposal_id)
                return 0 if p is not None and p[0].side.is_buy else 1
            all_events.sort(key=_buy_key)

            for evt in all_events:
                await self._handle_fill(evt)

            # Equity snapshot every 100 ticks
            if total_ticks % 100 == 0:
                mtm = self._portfolio.get_portfolio_mtm()
                self._equity.append((ts, mtm.total_equity_usdc))

            # Check kill switch periodically (every 50 ticks)
            if total_ticks % self._kill_switch_check_interval == 0:
                mtm = self._portfolio.get_portfolio_mtm()
                current_equity = mtm.total_equity_usdc
                peak_equity = self._portfolio.peak_equity
                drawdown = _drawdown(peak_equity, current_equity)

                if drawdown >= self._risk._limits.drawdown_kill_pct:
                    logger.critical(
                        "Backtest kill switch triggered: %.2f%% drawdown >= %.2f%% limit",
                        drawdown * 100,
                        self._risk._limits.drawdown_kill_pct * 100,
                    )
                    break

        # Final equity point
        mtm = self._portfolio.get_portfolio_mtm()
        self._equity.append((end_ts, mtm.total_equity_usdc))

        await self._portfolio.stop()

        # Finalize arb trades - call notify_arb_cleared() for completed arb groups
        self._finalize_arb()

        return self._build_result(start_ts, end_ts, total_ticks, list(self._streams.keys()))

    async def _handle_fill(self, evt: SimFill) -> None:
        pending = self._pending.get(evt.proposal_id)
        if pending is None:
            return

        proposal, submit_ts = pending

        # Record fill in portfolio
        fill_ratio = min(1.0, evt.filled_usdc / proposal.size_usdc) if proposal.size_usdc > 0 else 0.0
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
            try:
                await self._portfolio.record_fill(fill)
            except NegativeHoldings:
                logger.warning(
                    "Backtest fill skipped: NegativeHoldings for %s %s %s (filled=$%.2f @ %.4f)",
                    proposal.proposal_id[:8], proposal.side.value, proposal.market_id,
                    evt.filled_usdc, evt.fill_price,
                )
                self._sim.cancel(evt.proposal_id)
                await self._risk.notify_terminal(evt.proposal_id, proposal.platform, proposal.size_usdc)
                self._pending.pop(evt.proposal_id, None)
                return

            # Release strategy budget and notify arb clearing if complete
            if proposal.strategy_id == StrategyId.ARB:
                # For ARB, release budget on fill but wait for full completion to clear
                if fill_ratio >= 0.999:  # Fully filled
                    self._se.notify_arb_terminal(evt.filled_usdc)
                # Note: notify_arb_cleared() called in _build_result after all fills
            else:
                self._se.notify_mm_terminal(evt.filled_usdc)

        # Release risk reservation only on terminal fill events (Bug #24)
        if evt.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            await self._risk.notify_terminal(evt.proposal_id, proposal.platform, proposal.size_usdc)

        # Record trade
        latency_ms = evt.sim_ts - submit_ts if evt.sim_ts > submit_ts else None

        self._trades.append(
            TradeRecord(
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
            )
        )

        if evt.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            # Track completed arb groups before popping (Bug #32)
            if proposal.strategy_id == StrategyId.ARB and fill_ratio >= 0.999 and proposal.leg_group_id:
                self._arb_completed_groups.add((proposal.market_id, proposal.leg_group_id))
            self._pending.pop(evt.proposal_id, None)

    def _finalize_arb(self) -> None:
        """Call notify_arb_cleared() for completed ARB trades."""
        for market_id, leg_group_id in self._arb_completed_groups:
            self._se.notify_arb_cleared(market_id, leg_group_id)

    def _build_result(
        self,
        start_ts: int,
        end_ts: int,
        total_ticks: int,
        market_ids: List[str],
    ) -> BacktestResult:
        mtm = self._portfolio.get_portfolio_mtm()
        final_eq = mtm.total_equity_usdc
        total_pnl = final_eq - self._initial
        total_ret = total_pnl / self._initial if self._initial > 0 else 0.0

        snap = self._portfolio.build_snapshot()
        realised_pnl = snap["total_realised_pnl"]
        unrealised = total_pnl - realised_pnl

        approved = [t for t in self._trades if t.risk_verdict == "approved"]
        filled = [t for t in approved if t.fill_ratio >= 0.999]
        partial = [t for t in approved if 0 < t.fill_ratio < 0.999]
        expired = [t for t in approved if t.fill_ratio == 0.0 and t.fill_ts is not None]

        avg_fr = statistics.mean(t.fill_ratio for t in approved) if approved else 0.0
        slips = [t.slippage_bps for t in approved if t.slippage_bps is not None]
        avg_sl = statistics.mean(slips) if slips else None
        fr = (len(filled) + len(partial)) / max(1, len(approved))

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
            reject_reasons=self._reject_reasons,
            no_trade_expected=False,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic data generator
# ─────────────────────────────────────────────────────────────────────────────


def build_synthetic_tick_stream(
    market_id: str,
    n_ticks: int = 1_000,
    start_ts_ms: int = -1,  # -1 = use current wall-clock time
    tick_interval_ms: int = 500,
    initial_pm_mid: float = 0.50,
    initial_op_mid: float = 0.50,
    pm_bias: float = 0.0,
    op_bias: float = 0.0,
    vol: float = 0.005,
    spread: float = 0.012,
    pm_fee_bps: int = 20,
    op_fee_bps: int = 25,
    seed: Optional[int] = None,
) -> List[Tuple[int, MarketSnapshot, MarketSnapshot]]:
    """
    Generate a correlated random-walk tick stream for backtesting.

    The SAME event is modelled at the SAME initial mid on both venues
    (``initial_pm_mid == initial_op_mid`` by default). Because cross-venue
    arbitrage only exists when the two venues disagree on the same event,
    genuine arb opportunities in this generator arise only from transient,
    independent dislocations in the correlated random walk — not from a
    permanent hardcoded spread. This keeps backtest P&L honest.

    To stress-test with persistent mispricing, pass non-zero ``pm_bias`` /
    ``op_bias`` (e.g. ``pm_bias=-0.03`` to make Polymarket systematically
    cheaper). This is for adversarial testing only, not a realistic baseline.

    - Both venues share a common price shock (70% correlated)
    - Mean-reversion toward 0.5 (plus optional persistent bias)
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
    ts = start_ts_ms

    for i in range(n_ticks):
        # Correlated mean-reverting random walk. Targets include the optional
        # persistent bias so the venues can diverge systematically if requested.
        rev_pm = 0.01 * ((0.5 + pm_bias) - pm)
        rev_op = 0.01 * ((0.5 + op_bias) - op)
        common = random.gauss(0, vol)
        pm = max(0.02, min(0.98, pm + 0.7 * common + 0.3 * random.gauss(0, vol) + rev_pm))
        op = max(0.02, min(0.98, op + 0.7 * common + 0.3 * random.gauss(0, vol) + rev_op))

        d_pm = random.uniform(200, 2000)
        d_op = random.uniform(200, 2000)
        days = max(0.1, 10.0 - i * 10.0 / n_ticks)

        def _s(mid: float, depth: float, plat: Platform, fee: int) -> MarketSnapshot:
            no_mid = 1.0 - mid
            yes_bid = max(0.01, round(mid - spread / 2, 4))
            yes_ask = min(0.99, round(mid + spread / 2, 4))
            no_bid = max(0.01, round(no_mid - spread / 2, 4))
            no_ask = min(0.99, round(no_mid + spread / 2, 4))
            return MarketSnapshot(
                market_id=market_id,
                platform=plat,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                bid_depth_usdc=depth,
                ask_depth_usdc=depth * 0.9,
                taker_fee_bps=fee,
                ts=ts,
                received_ts=ts + random.randint(1, 8),
                days_to_resolution=days,
            )

        ticks.append(
            (
                ts,
                _s(pm, d_pm, Platform.POLYMARKET, pm_fee_bps),
                _s(op, d_op, Platform.OPINION, op_fee_bps),
            )
        )
        ts += tick_interval_ms

    return ticks


# ─────────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ─────────────────────────────────────────────────────────────────────────────


def _returns(series: List[Tuple[int, float]]) -> List[float]:
    return [(series[i][1] - series[i - 1][1]) / series[i - 1][1] for i in range(1, len(series)) if series[i - 1][1] > 0]


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
    span_ms = series[-1][0] - series[0][0]
    n = len(series)
    ms_per_y = 365.25 * 24 * 3600 * 1000
    return max(1.0, n / span_ms * ms_per_y) if span_ms > 0 else 252.0


def _sharpe(series: List[Tuple[int, float]], rfr: float = 0.0) -> Optional[float]:
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


def _sortino(series: List[Tuple[int, float]], rfr: float = 0.0) -> Optional[float]:
    r = _returns(series)
    if len(r) < 10:
        return None
    down = [x for x in r if x < 0]
    if len(down) < 2:
        return None
    try:
        m = statistics.mean(r)
        ds = statistics.stdev(down)
        if ds == 0:
            return None
        n = _periods_per_year(series)
        return (m - rfr / n) / ds * math.sqrt(n)
    except statistics.StatisticsError:
        return None
