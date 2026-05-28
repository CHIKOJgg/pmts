"""engine/orchestrator.py — Wires all components into the 5-step trading pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Dict, Optional, Tuple

from ai.enhancer import AISignalEnhancer
from data.market_data_provider import MarketDataProvider
from data.models import FeatureVector
from engine.feature_engine import FeatureEngine
from engine.strategy_engine import StrategyEngine
from execution.engine import ExecutionEngine
from execution.models import ExecutionResult, OrderProposal, OrderSubmission
from infrastructure.observability import PROPOSALS_TOTAL
from portfolio.manager import FillRecord, PortfolioManager
from risk.engine import RiskEngine
from src.clock import Clock, LiveClock
from src.types import ArbLeg, Platform, StrategyId

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Central coordinator for the PMTS trading system.

    Pipeline per tick:
      1. DATA       MarketDataProvider delivers MarketSnapshot
      2. FEATURES   FeatureEngine computes FeatureVector
      3. STRATEGY   StrategyEngine emits OrderProposals
      4. RISK       RiskEngine evaluates (synchronous gate, < 5ms)
      5. EXECUTION  ExecutionEngine submits to exchange
      6. PORTFOLIO  PortfolioManager.record_fill() on every fill
    """

    def __init__(
        self,
        mdp: MarketDataProvider,
        portfolio: PortfolioManager,
        risk: RiskEngine,
        strategy: StrategyEngine,
        pm_engine: ExecutionEngine,
        op_engine: ExecutionEngine,
        markets: list,
        ai_enhancer: Optional[AISignalEnhancer] = None,
        enable_trading: bool = True,
        clock: Optional[Clock] = None,
    ) -> None:
        self._mdp = mdp
        self._portfolio = portfolio
        self._risk = risk
        self._strategy = strategy
        self._pm_engine = pm_engine
        self._op_engine = op_engine
        self._ai = ai_enhancer
        self._markets = markets
        self._trading = enable_trading
        self._clock = clock or LiveClock()

        # Feature engine sits between MDP and StrategyEngine
        self._fe = FeatureEngine(portfolio=portfolio, clock=self._clock)

        # ── Wire the pipeline ─────────────────────────────────────────────────
        self._mdp.add_callback(self._fe.on_snapshot)
        self._fe.add_callback(self._on_feature_vector)
        self._strategy.add_proposal_callback(self._on_proposal)
        self._pm_engine.add_result_callback(self._on_execution_result)
        self._op_engine.add_result_callback(self._on_execution_result)

        # proposal_id → (strategy_id, size_usdc, platform, leg_group_id)
        self._in_flight: Dict[str, Tuple[StrategyId, float, Platform, Optional[str]]] = {}

        # leg_group_id → {leg_number_value: proposal_id | "market_id": str | "leg2_proposal": OrderProposal}
        self._arb_groups: Dict[str, Dict[Any, Any]] = {}

        # Per-market lock to prevent concurrent evaluation races
        self._market_locks: Dict[str, asyncio.Lock] = {}

        # Lock for arb_groups and in_flight modifications
        self._execution_lock: asyncio.Lock = asyncio.Lock()

        self._background_tasks: set[asyncio.Task] = set()

        # Metrics
        self.proposals_evaluated: int = 0
        self.proposals_approved: int = 0
        self.proposals_rejected: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("Orchestrator starting (%d markets)...", len(self._markets))
        await self._portfolio.start()
        await self._pm_engine.start()
        await self._op_engine.start()
        await self._mdp.start()
        logger.info(
            "Orchestrator started. trading=%s markets=%s",
            self._trading,
            self._markets,
        )

    async def stop(self) -> None:
        logger.info("Orchestrator stopping...")
        await self._mdp.stop()
        await self._pm_engine.stop()
        await self._op_engine.stop()
        await self._portfolio.stop()
        logger.info("Orchestrator stopped.")

    def get_active_markets(self) -> list[str]:
        return list(self._markets)

    def remove_market(self, market_id: str) -> bool:
        if market_id not in self._markets:
            return False
        self._markets = [m for m in self._markets if m != market_id]
        return True

    async def cancel_market_orders(self, market_id: str) -> int:
        cancelled = 0
        for pid, (_, _, platform, _) in list(self._in_flight.items()):
            tracker = self._pm_engine.get_tracker(pid) or self._op_engine.get_tracker(pid)
            if tracker is None or tracker.submission.market_id != market_id:
                continue
            engine = self._pm_engine if platform == Platform.POLYMARKET else self._op_engine
            await engine.cancel(pid)
            cancelled += 1
        return cancelled

    async def handle_market_resolution(self, market_id: str, outcome: Optional[str] = None) -> int:
        removed = self.remove_market(market_id)
        cancelled = await self.cancel_market_orders(market_id)
        logger.critical(
            "Market resolved market=%s outcome=%s removed=%s cancelled=%d",
            market_id,
            outcome or "unknown",
            removed,
            cancelled,
        )
        return cancelled

    async def emergency_stop(self, reason: str) -> None:
        logger.critical("EMERGENCY STOP: %s", reason)
        self._risk.manual_activate(reason)
        await self._cancel_all_open_orders()

    # ── Step 2→3: Feature vector → strategies ────────────────────────────────

    def _on_kill_switch_reset(self) -> None:
        self._arb_groups.clear()
        self._in_flight.clear()
        self._strategy.flush_market_state()
        logger.warning("Kill switch reset - arb groups and in-flight cleared")

    async def _on_feature_vector(self, fv: FeatureVector) -> None:
        if self._risk.kill_switch_active:
            return

        # Lock per market to prevent concurrent evaluation races (Issue #1)
        if fv.market_id not in self._market_locks:
            self._market_locks[fv.market_id] = asyncio.Lock()

        async with self._market_locks[fv.market_id]:
            await self._strategy.on_feature_vector(fv)

    # ── Step 3→4: Proposal → risk gate ───────────────────────────────────────

    async def _on_proposal(self, proposal: OrderProposal) -> None:
        self.proposals_evaluated += 1

        decision = self._risk.evaluate(proposal)  # synchronous, < 5ms
        verdict = "approved" if decision.approved else "rejected"
        PROPOSALS_TOTAL.labels(strategy=proposal.strategy_id.value, verdict=verdict).inc()

        if decision.rejected:
            self.proposals_rejected += 1
            if decision.kill_switch_active:
                task = asyncio.create_task(
                    self._kill_switch_response(),
                    name="kill-switch-response",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return

        if not self._trading:
            logger.info(
                "DRY RUN: would submit proposal=%s strategy=%s market=%s $%.2f side=%s",
                proposal.proposal_id[:8],
                proposal.strategy_id.value,
                proposal.market_id,
                proposal.size_usdc,
                proposal.side.value,
            )
            await self._risk.notify_terminal(proposal.proposal_id, proposal.platform, proposal.size_usdc)
            self.proposals_rejected += 1
            return

        self.proposals_approved += 1
        try:
            await self._route_to_engine(proposal)
        except Exception:
            await self._risk.notify_terminal(proposal.proposal_id, proposal.platform, proposal.size_usdc)
            self.proposals_approved -= 1
            raise

    # ── Step 4: Route to ExecutionEngine ─────────────────────────────────────

    async def _route_to_engine(self, proposal: OrderProposal) -> None:
        engine = self._pm_engine if proposal.platform == Platform.POLYMARKET else self._op_engine

        token_qty = round(proposal.size_usdc / proposal.limit_price, 6)
        if token_qty <= 0:
            logger.warning("Zero token_qty for proposal %s — skipping", proposal.proposal_id[:8])
            await self._risk.notify_terminal(proposal.proposal_id, proposal.platform, proposal.size_usdc)
            return

        now = self._clock.now_ms()
        submission = OrderSubmission(
            order_id=str(uuid.uuid4()),
            proposal_id=proposal.proposal_id,
            market_id=proposal.market_id,
            platform=proposal.platform,
            side=proposal.side,
            size_usdc=proposal.size_usdc,
            limit_price=proposal.limit_price,
            order_type=proposal.order_type,
            strategy_id=proposal.strategy_id,
            expiry_ms=proposal.expiry_ms,
            token_quantity=token_qty,
            submitted_at=now,
            leg_group_id=proposal.leg_group_id,
            leg_number=proposal.leg_number,
            min_fill_ratio=proposal.min_fill_ratio,
        )

        self._in_flight[proposal.proposal_id] = (
            proposal.strategy_id,
            proposal.size_usdc,
            proposal.platform,
            proposal.leg_group_id,
        )

        if proposal.is_arb and proposal.leg_number is not None:
            grp = self._arb_groups.setdefault(proposal.leg_group_id, {"market_id": proposal.market_id})
            if proposal.leg_number == ArbLeg.LEG_2:
                # Step 4: Hold leg 2 until leg 1 confirms fill
                grp["leg2_proposal"] = proposal
                return
            else:
                grp[proposal.leg_number.value] = proposal.proposal_id

        await engine.submit(submission)

    # ── Step 5: ExecutionResult → portfolio update ────────────────────────────

    async def _on_execution_result(self, result: ExecutionResult) -> None:
        # Record fills
        if result.filled_size_usdc > 0 and result.fill_price is not None:
            info = self._in_flight.get(result.proposal_id)
            if info:
                _, _, platform, _ = info
                engine = self._pm_engine if platform == Platform.POLYMARKET else self._op_engine
                tracker = engine.get_tracker(result.proposal_id)
                if tracker:
                    fill = FillRecord(
                        proposal_id=result.proposal_id,
                        order_id=tracker.submission.order_id,
                        market_id=tracker.submission.market_id,
                        platform=tracker.submission.platform,
                        side=tracker.submission.side.value,
                        filled_usdc=result.filled_size_usdc,
                        fill_price=result.fill_price,
                        ts=result.ts,
                        strategy_id=tracker.submission.strategy_id.value,
                    )
                    await self._portfolio.record_fill(fill)

        # Release resources on terminal state
        if result.is_terminal:
            await self._on_terminal(result)

    async def _on_terminal(self, result: ExecutionResult) -> None:
        info = self._in_flight.pop(result.proposal_id, None)
        if info is None:
            return

        strategy_id, size_usdc, platform, leg_group_id = info

        await self._risk.notify_terminal(result.proposal_id, platform, size_usdc)

        if strategy_id == StrategyId.ARB:
            self._strategy.notify_arb_terminal(size_usdc)
        else:
            self._strategy.notify_mm_terminal(size_usdc)

        # Arb leg management
        if leg_group_id and strategy_id == StrategyId.ARB:
            await self._handle_arb_terminal(result, leg_group_id)

    async def _handle_arb_terminal(self, result: ExecutionResult, leg_group_id: str) -> None:
        grp = self._arb_groups.get(leg_group_id)
        if grp is None:
            return

        # Use stored market_id (set when arb group was created)
        market_id = grp.get("market_id")

        # Find our tracker for leg info
        tracker = None
        for eng in [self._pm_engine, self._op_engine]:
            t = eng.get_tracker(result.proposal_id)
            if t is not None:
                tracker = t
                break

        if tracker is None or tracker.submission.leg_number is None:
            return

        # If leg1 is terminal, decide whether to submit leg2
        if tracker.submission.leg_number == ArbLeg.LEG_1:
            leg2_proposal = grp.get("leg2_proposal")
            if leg2_proposal:
                min_ratio = tracker.submission.min_fill_ratio or 0.80
                actual_ratio = tracker.fill_ratio
                should_abort = actual_ratio < min_ratio

                if should_abort:
                    logger.warning(
                        "ARB leg1 fill_ratio=%.2f < min=%.2f — aborting leg2 %s",
                        actual_ratio,
                        min_ratio,
                        leg2_proposal.proposal_id[:8],
                    )
                    await self._risk.notify_terminal(
                        leg2_proposal.proposal_id, leg2_proposal.platform, leg2_proposal.size_usdc
                    )
                    self._strategy.notify_arb_terminal(leg2_proposal.size_usdc)
                    self._in_flight.pop(leg2_proposal.proposal_id, None)
                    grp.pop("leg2_proposal", None)
                else:
                    new_size_usdc = leg2_proposal.size_usdc * actual_ratio
                    token_qty = round(new_size_usdc / leg2_proposal.limit_price, 6)
                    if token_qty > 0:
                        submission = OrderSubmission(
                            order_id=str(uuid.uuid4()),
                            proposal_id=leg2_proposal.proposal_id,
                            market_id=leg2_proposal.market_id,
                            platform=leg2_proposal.platform,
                            side=leg2_proposal.side,
                            size_usdc=new_size_usdc,
                            limit_price=leg2_proposal.limit_price,
                            order_type=leg2_proposal.order_type,
                            strategy_id=leg2_proposal.strategy_id,
                            expiry_ms=leg2_proposal.expiry_ms,
                            token_quantity=token_qty,
                            submitted_at=self._clock.now_ms(),
                            leg_group_id=leg2_proposal.leg_group_id,
                            leg_number=leg2_proposal.leg_number,
                            min_fill_ratio=leg2_proposal.min_fill_ratio,
                        )
                        engine = self._pm_engine if leg2_proposal.platform == Platform.POLYMARKET else self._op_engine
                        grp[ArbLeg.LEG_2.value] = leg2_proposal.proposal_id
                        grp.pop("leg2_proposal", None)
                        await engine.submit(submission)
                    else:
                        await self._risk.notify_terminal(
                            leg2_proposal.proposal_id, leg2_proposal.platform, leg2_proposal.size_usdc
                        )
                        self._strategy.notify_arb_terminal(leg2_proposal.size_usdc)
                        self._in_flight.pop(leg2_proposal.proposal_id, None)
                        grp.pop("leg2_proposal", None)

        # Check if all submitted legs are terminal → clear arb_in_flight
        submitted_pids = [pid for k, pid in grp.items() if isinstance(k, int)]

        # Also check if leg2_proposal exists and is terminal (was never submitted due to abort)
        pending_leg2 = grp.get("leg2_proposal")
        all_terminal = False

        if not submitted_pids:
            # No legs were submitted at all
            all_terminal = True
        elif pending_leg2 is not None:
            # Leg1 was submitted, leg2 never was (aborted)
            # Check if leg1 is terminal
            all_terminal = (
                len(submitted_pids) == 1
                and (
                    self._pm_engine.get_tracker(submitted_pids[0])
                    or self._op_engine.get_tracker(submitted_pids[0])
                    or _NullTracker()
                ).status.is_terminal
            )
        else:
            # Both legs were submitted, check both are terminal
            all_terminal = len(submitted_pids) == 2 and all(
                (
                    self._pm_engine.get_tracker(pid) or self._op_engine.get_tracker(pid) or _NullTracker()
                ).status.is_terminal
                for pid in submitted_pids
            )

        if all_terminal and market_id:
            self._strategy.notify_arb_cleared(market_id, leg_group_id)
            self._arb_groups.pop(leg_group_id, None)

    async def _kill_switch_response(self) -> None:
        logger.critical("Kill switch response: cancelling all open orders")
        await self._cancel_all_open_orders()

    async def _cancel_all_open_orders(self) -> None:
        for pid, (_, _, platform, _) in list(self._in_flight.items()):
            engine = self._pm_engine if platform == Platform.POLYMARKET else self._op_engine
            try:
                await engine.cancel(pid)
            except Exception as exc:
                logger.error("Cancel failed for %s: %s", pid[:8], exc)


class _NullTracker:
    """Sentinel used when tracker lookup fails — always reports terminal."""

    class _S:
        is_terminal = True

    status = _S()


def _now_ms() -> int:
    return int(time.time() * 1000)
