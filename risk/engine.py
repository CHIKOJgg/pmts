"""
risk/engine.py — Synchronous pre-trade risk gate.

Critical design: capital is reserved SYNCHRONOUSLY inside evaluate() before
it returns. This eliminates the TOCTOU window where two proposals could both
pass the capital check before either reservation is recorded.

Strategy: RiskEngine keeps its own _reservations dict. available capital =
cash − sum(reservations.values()). No async, no race.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Callable, Dict, Optional, Tuple

from execution.models import OrderProposal
from infrastructure.observability import CAPITAL_UTILIZATION, DRAWDOWN_PCT, KILL_SWITCH_ACTIVE
from portfolio.manager import PortfolioManager
from risk.kill_switch import KillSwitch
from risk.limits import DEFAULT_LIMITS, RiskLimits
from src.clock import Clock, LiveClock
from src.types import (
    ConnectorStatus,
    Platform,
    RejectReason,
    RiskVerdict,
    Side,
    StrategyId,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# RiskDecision — immutable result
# ─────────────────────────────────────────────────────────────────────────────


class RiskDecision:
    __slots__ = (
        "proposal_id",
        "verdict",
        "reject_reason",
        "reject_detail",
        "capital_available",
        "capital_reserved",
        "mtm_drawdown_pct",
        "peak_equity_usdc",
        "current_equity_usdc",
        "kill_switch_active",
        "decided_at",
    )

    def __init__(
        self,
        proposal_id: str,
        verdict: RiskVerdict,
        reject_reason: Optional[RejectReason],
        reject_detail: Optional[str],
        capital_available: float,
        capital_reserved: float,
        mtm_drawdown_pct: float,
        peak_equity_usdc: float,
        current_equity_usdc: float,
        kill_switch_active: bool,
        decided_at: int,
    ) -> None:
        for attr in self.__slots__:
            object.__setattr__(self, attr, locals()[attr])

    @property
    def approved(self) -> bool:
        return self.verdict == RiskVerdict.APPROVED

    @property
    def rejected(self) -> bool:
        return self.verdict == RiskVerdict.REJECTED


# ─────────────────────────────────────────────────────────────────────────────
# RiskEngine
# ─────────────────────────────────────────────────────────────────────────────


class RiskEngine:
    """
    Synchronous pre-trade risk gate. All evaluate() calls complete in < 5 ms.

    12 checks in priority order:
      1  Kill switch active
      2  Connector DOWN
      3  MTM drawdown ≥ kill threshold (also activates kill switch)
      4  MTM drawdown ≥ warn threshold (log only, no block)
      5  Duplicate proposal_id
      6  Order size < minimum
      7  Order size > maximum
      8  Liquidity buffer breach
      9  Insufficient capital (after all outstanding reservations)
      10 Per-market exposure limit
      11 Per-strategy capital cap
      12 Projected delta limit
    """

    def __init__(
        self,
        portfolio: PortfolioManager,
        kill_switch: KillSwitch,
        limits: RiskLimits = DEFAULT_LIMITS,
        connector_status_fn: Optional[Callable[[Platform], ConnectorStatus]] = None,
        stream_writer: Optional[Callable] = None,
        store=None,
        alert_router=None,
        clock: Optional[Clock] = None,
    ) -> None:
        self._portfolio = portfolio
        self._kill_switch = kill_switch
        self._limits = limits
        self._connector_status = connector_status_fn
        self._stream_writer = stream_writer
        self._store = store
        self._alert_router = alert_router
        self._clock = clock or LiveClock()

        # Synchronous reservation table — the ONLY source of truth for committed capital
        # proposal_id -> (amount, platform, strategy_id)
        self._reservations: Dict[str, Tuple[float, Platform, StrategyId]] = {}
        self._arb_allocated: float = 0.0
        self._mm_allocated: float = 0.0

        # Lock for atomic capital reservation operations (sync — all calls from event loop)
        import threading

        self._capital_lock: threading.Lock = threading.Lock()

        if self._store:
            loaded_res = self._store.load_reservations()
            for pid, (amt, plat, strat) in loaded_res.items():
                self._reservations[pid] = (amt, plat, strat)
                if strat == StrategyId.ARB:
                    self._arb_allocated += amt
                else:
                    self._mm_allocated += amt
            if loaded_res:
                logger.info("Loaded %d risk reservations from SQLite", len(loaded_res))

            # Load persistent kill switch state
            if self._store.load_kill_switch():
                logger.warning("Restoring ACTIVE kill switch state from SQLite")
                self._kill_switch.sync_state(True)

        self.reconciliation_complete: bool = False

        # LRU dedup cache
        self._dedup: collections.OrderedDict[str, int] = collections.OrderedDict()
        self._on_kill_switch_reset: Optional[Callable[[], None]] = None

        self.total_evaluated: int = 0
        self.total_approved: int = 0
        self.total_rejected: int = 0
        self.rejections_by_reason: Dict[str, int] = {r.value: 0 for r in RejectReason}

    # ── Primary interface ─────────────────────────────────────────────────────

    def evaluate(self, proposal: OrderProposal) -> RiskDecision:
        """
        Run all 12 risk checks synchronously.
        Capital reserved before this method returns.
        Must complete in < 5 ms.
        """
        self.total_evaluated += 1
        now = self._clock.now_ms()
        mtm_age_ms = self._portfolio.get_price_age_ms()
        if mtm_age_ms > self._limits.max_mtm_age_ms:
            return self._reject(
                proposal,
                RejectReason.STALE_MTM,
                f"MTM price {mtm_age_ms}ms old > limit {self._limits.max_mtm_age_ms}ms",
                available=0.0,
                committed=sum(r[0] for r in self._reservations.values()),
                drawdown=0.0,
                peak=self._portfolio.peak_equity,
                equity=self._portfolio.peak_equity,
                now=now,
            )
        mtm = self._portfolio.get_portfolio_mtm()
        peak = self._portfolio.peak_equity
        equity = mtm.total_equity_usdc
        cash = self._portfolio.cash_usdc

        # Available capital = cash minus ALL outstanding reservations (sync dict read)
        committed = sum(r[0] for r in self._reservations.values())
        available = max(0.0, cash - committed)
        drawdown = _drawdown(peak, equity)

        DRAWDOWN_PCT.set(drawdown)
        KILL_SWITCH_ACTIVE.set(1.0 if self._kill_switch.is_active else 0.0)
        CAPITAL_UTILIZATION.set(committed / equity if equity > 0 else 0.0)

        def reject(reason: RejectReason, detail: str) -> RiskDecision:
            return self._reject(
                proposal,
                reason,
                detail,
                available,
                committed,
                drawdown,
                peak,
                equity,
                now,
            )

        # ── 1. Kill switch ────────────────────────────────────────────────────
        if self._kill_switch.is_active:
            return reject(RejectReason.KILL_SWITCH_ACTIVE, "Kill switch active")

        # ── 2. Connector DOWN ─────────────────────────────────────────────────
        if self._connector_status is not None:
            cs = self._connector_status(proposal.platform)
            if cs == ConnectorStatus.DOWN:
                return reject(
                    RejectReason.CONNECTOR_DOWN,
                    f"{proposal.platform.value} connector is DOWN",
                )

        # ── 3. Drawdown kill ──────────────────────────────────────────────────
        if drawdown >= self._limits.drawdown_kill_pct:
            self._fire_kill_switch(drawdown, peak, equity, proposal.proposal_id)
            return reject(
                RejectReason.DRAWDOWN_LIMIT,
                f"Drawdown {drawdown:.2%} ≥ kill {self._limits.drawdown_kill_pct:.2%}",
            )

        # ── 4. Drawdown warn ──────────────────────────────────────────────────
        if drawdown >= self._limits.drawdown_warn_pct:
            logger.warning("DRAWDOWN WARNING: %.2f%%", drawdown * 100)

        # ── 5. Duplicate ──────────────────────────────────────────────────────
        if self._is_duplicate(proposal.proposal_id, now):
            return reject(
                RejectReason.DUPLICATE_PROPOSAL,
                "Duplicate proposal_id within dedup window",
            )
        self._record_seen(proposal.proposal_id, now)

        # ── 6. Order size min ─────────────────────────────────────────────────
        if proposal.size_usdc < self._limits.min_single_order_usdc:
            return reject(
                RejectReason.ORDER_TOO_SMALL,
                f"${proposal.size_usdc:.2f} < min ${self._limits.min_single_order_usdc:.2f}",
            )

        # ── 7. Order size max ─────────────────────────────────────────────────
        if proposal.size_usdc > self._limits.max_single_order_usdc:
            return reject(
                RejectReason.ORDER_TOO_LARGE,
                f"${proposal.size_usdc:.2f} > max ${self._limits.max_single_order_usdc:.2f}",
            )

        # ── 8. Liquidity buffer ───────────────────────────────────────────────
        min_free = equity * self._limits.min_free_capital_pct
        if available - proposal.size_usdc < min_free:
            return reject(
                RejectReason.LIQUIDITY_BUFFER,
                f"Would breach liquidity buffer (min_free=${min_free:.2f})",
            )

        # ── 9. Capital available ──────────────────────────────────────────────
        if proposal.size_usdc > available:
            return reject(
                RejectReason.INSUFFICIENT_CAPITAL,
                f"Need ${proposal.size_usdc:.2f}, available ${available:.2f}",
            )

        # ── 10. Market exposure ───────────────────────────────────────────────
        market_exp = self._portfolio.get_market_exposure_usdc(proposal.market_id)
        max_market = min(
            self._limits.max_market_exposure_usdc,
            equity * self._limits.max_market_exposure_pct,
        )
        if market_exp + proposal.size_usdc > max_market:
            return reject(
                RejectReason.MARKET_EXPOSURE_LIMIT,
                f"Market exposure ${market_exp + proposal.size_usdc:.2f} > ${max_market:.2f}",
            )

        # ── 11. Strategy cap ──────────────────────────────────────────────────
        strat_used = self._arb_allocated if proposal.strategy_id == StrategyId.ARB else self._mm_allocated
        strat_cap = (
            self._limits.max_arb_capital_usdc
            if proposal.strategy_id == StrategyId.ARB
            else self._limits.max_mm_capital_usdc
        )
        if strat_used + proposal.size_usdc > strat_cap:
            return reject(
                RejectReason.STRATEGY_CAP_EXCEEDED,
                f"{proposal.strategy_id.value} cap ${strat_cap:.2f} exceeded",
            )

        # ── 12. Projected delta ───────────────────────────────────────────────
        current_delta = self._portfolio.get_delta(proposal.market_id).net_delta
        projected_delta = _projected_delta(current_delta, proposal)
        if abs(projected_delta) > self._limits.max_net_delta_per_market:
            return reject(
                RejectReason.DELTA_LIMIT,
                f"Projected |Δ|={abs(projected_delta):.2f} > limit {self._limits.max_net_delta_per_market:.2f}",
            )

        # ── APPROVED — reserve capital atomically under lock ──────────────────
        with self._capital_lock:
            self._reservations[proposal.proposal_id] = (proposal.size_usdc, proposal.platform, proposal.strategy_id)
            if proposal.strategy_id == StrategyId.ARB:
                self._arb_allocated += proposal.size_usdc
            else:
                self._mm_allocated += proposal.size_usdc

        if self._store:
            self._store.save_reservation(
                proposal.proposal_id, proposal.size_usdc, proposal.platform, proposal.strategy_id
            )

        self.total_approved += 1
        return RiskDecision(
            proposal_id=proposal.proposal_id,
            verdict=RiskVerdict.APPROVED,
            reject_reason=None,
            reject_detail=None,
            capital_available=available - proposal.size_usdc,
            capital_reserved=committed + proposal.size_usdc,
            mtm_drawdown_pct=min(drawdown, 1.0),
            peak_equity_usdc=peak,
            current_equity_usdc=equity,
            kill_switch_active=False,
            decided_at=now,
        )

    # ── Terminal notification ─────────────────────────────────────────────────

    async def notify_terminal(self, proposal_id: str, platform: Platform, amount_usdc: float) -> None:
        """Release reservation when order reaches terminal state."""
        info = self._reservations.pop(proposal_id, None)
        if info is None:
            # Check if it's in the store (reconciliation case)
            if self._store:
                self._store.remove_reservation(proposal_id)
            return

        if self._store:
            self._store.remove_reservation(proposal_id)

        reserved_amount, _, strategy_id = info
        if strategy_id == StrategyId.ARB:
            self._arb_allocated = max(0.0, self._arb_allocated - reserved_amount)
        else:
            self._mm_allocated = max(0.0, self._mm_allocated - reserved_amount)
        await self._portfolio.release_capital(reserved_amount)

    def reconcile_reservations(self) -> None:
        """
        Sync SQLite reservations with memory and purge orphaned ones.
        Called after ExecutionEngine reconciliation.
        """
        if not self._store:
            return

        logger.info("Reconciling risk reservations...")
        db_res = self._store.load_reservations()

        # Memory is empty on startup, so we populate it from DB
        for pid, (amt, plat, strat) in db_res.items():
            if pid not in self._reservations:
                self._reservations[pid] = (amt, plat, strat)
                if strat == StrategyId.ARB:
                    self._arb_allocated += amt
                else:
                    self._mm_allocated += amt

        # Any PID in memory that is NOT in ExecutionEngine trackers (after its reconcile)
        # will be handled by the notify_terminal calls triggered by reconcile failures.
        self.reconciliation_complete = True

    # ── Kill switch control ───────────────────────────────────────────────────

    def manual_activate(self, reason: str = "operator_manual") -> None:
        mtm = self._portfolio.get_portfolio_mtm()
        peak = self._portfolio.peak_equity
        self._fire_kill_switch(
            _drawdown(peak, mtm.total_equity_usdc),
            peak,
            mtm.total_equity_usdc,
            None,
        )
        if self._store:
            self._store.save_kill_switch(True)

    def reset_kill_switch(self, confirmation_token: str, operator_id: Optional[str] = None) -> bool:
        success = self._kill_switch.reset(confirmation_token, operator_id)
        if success:
            for proposal_id in list(self._reservations.keys()):
                if self._store:
                    self._store.remove_reservation(proposal_id)
            self._reservations.clear()
            self._arb_allocated = 0.0
            self._mm_allocated = 0.0
            KILL_SWITCH_ACTIVE.set(0.0)
            if self._store:
                self._store.save_kill_switch(False)
            if self._on_kill_switch_reset:
                try:
                    self._on_kill_switch_reset()
                except Exception as exc:
                    logger.error("Kill switch reset callback failed: %s", exc, exc_info=True)
        return success

    def set_kill_switch_reset_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._on_kill_switch_reset = callback

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch.is_active

    @property
    def current_drawdown(self) -> float:
        """Return current portfolio drawdown calculated from peak equity."""
        mtm = self._portfolio.get_portfolio_mtm()
        peak = self._portfolio.peak_equity
        return _drawdown(peak, mtm.total_equity_usdc)

    def reload_limits(self, new_limits: RiskLimits) -> None:
        self._limits = new_limits
        logger.info("RiskEngine limits reloaded")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _reject(
        self,
        proposal: OrderProposal,
        reason: RejectReason,
        detail: str,
        available: float,
        committed: float,
        drawdown: float,
        peak: float,
        equity: float,
        now: int,
    ) -> RiskDecision:
        self.total_rejected += 1
        self.rejections_by_reason[reason.value] = self.rejections_by_reason.get(reason.value, 0) + 1
        logger.warning(
            "REJECT proposal=%s strategy=%s market=%s $%.2f reason=%s — %s",
            proposal.proposal_id[:8],
            proposal.strategy_id.value,
            proposal.market_id,
            proposal.size_usdc,
            reason.value,
            detail,
        )
        return RiskDecision(
            proposal_id=proposal.proposal_id,
            verdict=RiskVerdict.REJECTED,
            reject_reason=reason,
            reject_detail=detail,
            capital_available=available,
            capital_reserved=committed,
            mtm_drawdown_pct=min(drawdown, 1.0),
            peak_equity_usdc=peak,
            current_equity_usdc=equity,
            kill_switch_active=self._kill_switch.is_active,
            decided_at=now,
        )

    def _is_duplicate(self, pid: str, now: int) -> bool:
        cutoff = now - self._limits.dedup_window_s * 1000
        return pid in self._dedup and self._dedup[pid] > cutoff

    def _record_seen(self, pid: str, now: int) -> None:
        self._dedup[pid] = now
        self._dedup.move_to_end(pid)
        while len(self._dedup) > self._limits.dedup_cache_size:
            self._dedup.popitem(last=False)

    def _fire_kill_switch(self, drawdown: float, peak: float, equity: float, triggering_id: Optional[str]) -> None:
        record = self._kill_switch.activate(
            reason="drawdown_limit_breached",
            mtm_drawdown=drawdown,
            peak_equity=peak,
            current_equity=equity,
            triggering_id=triggering_id,
        )
        KILL_SWITCH_ACTIVE.set(1.0)
        if self._store:
            self._store.save_kill_switch(True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("No running event loop — cannot send kill-switch alerts")
            return

        if self._alert_router:
            from infrastructure.alerting import Alert, AlertSeverity

            alert = Alert(
                severity=AlertSeverity.CRITICAL,
                title="Kill Switch Activated",
                message=f"Drawdown {drawdown:.2%} exceeded kill threshold",
                source="RiskEngine",
                metadata={"drawdown": drawdown, "triggering_id": triggering_id},
            )
            loop.create_task(self._alert_router.send(alert))

        if self._stream_writer:
            loop.create_task(
                self._stream_writer(
                    "risk_events",
                    {
                        "event": "kill_switch_activated",
                        "activated_at": record.activated_at,
                        "drawdown": drawdown,
                        "triggering": triggering_id,
                    },
                )
            )


def _drawdown(peak: float, current: float) -> float:
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - current) / peak)


def _projected_delta(current_delta: float, proposal: OrderProposal) -> float:
    """Estimate net delta after full execution of this proposal."""
    qty = proposal.size_usdc / proposal.limit_price if proposal.limit_price > 0 else 0.0
    if proposal.side == Side.BUY_YES:
        return current_delta + qty
    elif proposal.side == Side.BUY_NO:
        return current_delta - qty
    elif proposal.side == Side.SELL_YES:
        return current_delta - qty
    else:  # SELL_NO
        return current_delta + qty


def _now_ms() -> int:
    return int(time.time() * 1000)
