"""engine/strategy_engine.py — Combines strategies, allocates capital, resolves conflicts."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Dict, List, Optional

from data.models import FeatureVector
from execution.models import OrderProposal
from src.types import Platform, StrategyId
from strategies.arbitrage import ArbitrageStrategy, ArbConfig
from strategies.delta_neutral import DeltaNeutralStrategy, DeltaNeutralConfig
from ai.enhancer import AISignalEnhancer, AIEnhancerConfig
from ai.signal_context import SignalContext

logger = logging.getLogger(__name__)

_ProposalCB = Callable[[OrderProposal], Coroutine]


@dataclass
class StrategyConfig:
    """Configuration for the combined StrategyEngine."""
    arb_budget_usdc:   float = 2_000.0
    mm_budget_usdc:    float = 3_000.0
    arb_cooldown_ms:   int   = 2_000      # same as arb expiry window
    mm_cooldown_ms:    int   = 500
    hedge_cooldown_ms: int   = 1_000
    arb_enabled:       bool  = True
    mm_enabled:        bool  = True
    hedge_enabled:     bool  = True
    mm_platforms:      List  = field(default_factory=lambda: list(Platform))


@dataclass
class _MarketState:
    last_arb_ts:   int            = 0
    last_mm_ts:    int            = 0
    last_hedge_ts: int            = 0
    arb_in_flight: bool           = False
    arb_group_id:  Optional[str]  = None


class StrategyEngine:
    """
    Orchestrates ArbitrageStrategy and DeltaNeutralStrategy.

    Emits OrderProposals in priority order:
      1. ARB proposals (leg1 then leg2)
      2. HEDGE proposals
      3. MM quotes (suppressed while any arb is in-flight on the same market)

    Capital budgets are soft pre-allocations — RiskEngine is the hard gate.
    Budget is released via notify_arb_terminal() / notify_mm_terminal().

    arb_in_flight is tracked per market and per leg_group_id.
    notify_arb_cleared() must be called when all legs of a group terminate,
    otherwise MM quoting is permanently suppressed for that market.
    """

    def __init__(
        self,
        config:     StrategyConfig      = StrategyConfig(),
        arb_config: ArbConfig           = ArbConfig(),
        dn_config:  DeltaNeutralConfig  = DeltaNeutralConfig(),
    ) -> None:
        self._config     = config
        self._arb        = ArbitrageStrategy(config=arb_config)
        self._dn         = DeltaNeutralStrategy(config=dn_config)
        # Instantiate AI Enhancer with heuristic-only fallback initially
        self._ai         = AISignalEnhancer(AIEnhancerConfig(use_heuristic_only=True))
        self._arb_alloc: float = 0.0
        self._mm_alloc:  float = 0.0
        self._market:    Dict[str, _MarketState] = {}
        self._callbacks: List[_ProposalCB] = []

        # Metrics
        self.arb_emitted:     int = 0
        self.mm_emitted:      int = 0
        self.hedge_emitted:   int = 0
        self.suppressed_cd:   int = 0
        self.suppressed_bud:  int = 0
        self.suppressed_cfl:  int = 0

    def add_proposal_callback(self, cb: _ProposalCB) -> None:
        self._callbacks.append(cb)

    async def on_feature_vector(
        self, fv: FeatureVector, now_ts: Optional[int] = None
    ) -> None:
        """
        Process one FeatureVector through all enabled strategies.

        now_ts: simulated current time (backtest).  None = wall-clock (live).
        """
        now = now_ts if now_ts is not None else _now_ms()
        st  = self._get_state(fv.market_id)
        proposals: List[OrderProposal] = []

        ctx = await self._ai.enhance(fv)

        if self._config.arb_enabled:
            proposals.extend(await self._eval_arb(fv, st, now, ctx))

        if self._config.hedge_enabled:
            hedge = await self._eval_hedge(fv, st, now, ctx)
            if hedge is not None:
                proposals.append(hedge)

        if self._config.mm_enabled and not st.arb_in_flight and not ctx.suppress_mm:
            proposals.extend(await self._eval_mm(fv, st, now, ctx))

        for p in proposals:
            await self._emit(p)

    # ── Budget feedback ───────────────────────────────────────────────────────

    def notify_arb_terminal(self, size_usdc: float) -> None:
        self._arb_alloc = max(0.0, self._arb_alloc - size_usdc)

    def notify_mm_terminal(self, size_usdc: float) -> None:
        self._mm_alloc = max(0.0, self._mm_alloc - size_usdc)

    def notify_arb_cleared(self, market_id: str, leg_group_id: str) -> None:
        """Call when BOTH legs of an arb group have reached terminal state."""
        st = self._market.get(market_id)
        if st and st.arb_group_id == leg_group_id:
            st.arb_in_flight = False
            st.arb_group_id  = None

    def reload_configs(
        self,
        arb_config:      Optional[ArbConfig]          = None,
        dn_config:       Optional[DeltaNeutralConfig]  = None,
        strategy_config: Optional[StrategyConfig]      = None,
    ) -> None:
        if arb_config:
            self._arb.reload_config(arb_config)
        if dn_config:
            self._dn.reload_config(dn_config)
        if strategy_config:
            self._config = strategy_config

    @property
    def arb_available_budget(self) -> float:
        return max(0.0, self._config.arb_budget_usdc - self._arb_alloc)

    @property
    def mm_available_budget(self) -> float:
        return max(0.0, self._config.mm_budget_usdc - self._mm_alloc)

    # ── Internal evaluators ───────────────────────────────────────────────────

    async def _eval_arb(
        self, fv: FeatureVector, st: _MarketState, now: int, ctx: SignalContext
    ) -> List[OrderProposal]:
        if now - st.last_arb_ts < self._config.arb_cooldown_ms:
            self.suppressed_cd += 1
            return []
        if st.arb_in_flight:
            self.suppressed_cfl += 1
            return []

        result = self._arb.evaluate(fv, now_ts=now, ctx=ctx)
        if not result.accepted or result.leg1_proposal is None:
            return []

        total_needed = result.leg1_proposal.size_usdc * 2
        if total_needed > self.arb_available_budget:
            self.suppressed_bud += 1
            return []

        self._arb_alloc    += total_needed
        st.last_arb_ts      = now
        st.arb_in_flight    = True
        st.arb_group_id     = result.leg1_proposal.leg_group_id
        self.arb_emitted   += 2
        return [result.leg1_proposal, result.leg2_proposal]

    async def _eval_hedge(
        self, fv: FeatureVector, st: _MarketState, now: int, ctx: SignalContext
    ) -> Optional[OrderProposal]:
        if now - st.last_hedge_ts < self._config.hedge_cooldown_ms:
            return None

        result = self._dn.evaluate_hedge(fv, ctx=ctx)
        if not result.should_hedge or result.proposal is None:
            return None

        size = result.proposal.size_usdc
        if size > self.mm_available_budget:
            self.suppressed_bud += 1
            return None

        self._mm_alloc    += size
        st.last_hedge_ts   = now
        self.hedge_emitted += 1
        return result.proposal

    async def _eval_mm(
        self, fv: FeatureVector, st: _MarketState, now: int, ctx: SignalContext
    ) -> List[OrderProposal]:
        if now - st.last_mm_ts < self._config.mm_cooldown_ms:
            return []

        per_side    = self._dn._cfg.mm_quote_size_usdc
        total_need  = per_side * 2 * len(self._config.mm_platforms)
        if total_need > self.mm_available_budget:
            self.suppressed_bud += 1
            return []

        proposals: List[OrderProposal] = []
        for platform in self._config.mm_platforms:
            result = self._dn.evaluate_mm(fv, platform, ctx=ctx)
            if result.suppressed:
                continue
            if result.bid_proposal:
                proposals.append(result.bid_proposal)
                self._mm_alloc += result.bid_proposal.size_usdc
                self.mm_emitted += 1
            if result.ask_proposal:
                proposals.append(result.ask_proposal)
                self._mm_alloc += result.ask_proposal.size_usdc
                self.mm_emitted += 1

        if proposals:
            st.last_mm_ts = now
        return proposals

    def _get_state(self, market_id: str) -> _MarketState:
        if market_id not in self._market:
            self._market[market_id] = _MarketState()
        return self._market[market_id]

    async def _emit(self, proposal: OrderProposal) -> None:
        for cb in self._callbacks:
            try:
                await cb(proposal)
            except Exception as exc:
                logger.error("Proposal callback raised: %s", exc, exc_info=True)


def _now_ms() -> int:
    return int(time.time() * 1000)