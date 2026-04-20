"""execution/models.py — Order lifecycle data models."""
from __future__ import annotations

import time
import uuid as _uuid_mod
from dataclasses import dataclass, field, asdict
from typing import Optional

from src.types import (
    ArbLeg, EpochMs, FillRatio, OrderStatus, OrderType,
    Platform, Side, StrategyId,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _model_dump(obj) -> dict:
    d = asdict(obj)
    # Convert enums to their values
    for k, v in d.items():
        if hasattr(v, 'value'):
            d[k] = v.value
    return d


# ─────────────────────────────────────────────────────────────────────────────
# OrderProposal — intent from StrategyEngine, not yet risk-checked
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderProposal:
    proposal_id:    str
    market_id:      str
    platform:       Platform
    side:           Side
    size_usdc:      float
    limit_price:    float
    order_type:     OrderType
    strategy_id:    StrategyId
    expiry_ms:      int
    source_ts:      int
    leg_group_id:   Optional[str]      = None
    leg_number:     Optional[ArbLeg]   = None
    min_fill_ratio: Optional[float]    = None

    def __post_init__(self):
        if self.size_usdc <= 0:
            raise ValueError(f"size_usdc must be > 0, got {self.size_usdc}")
        if not (0.001 <= self.limit_price <= 0.999):
            raise ValueError(f"limit_price must be in [0.001, 0.999], got {self.limit_price}")
        if self.strategy_id == StrategyId.ARB:
            if self.leg_group_id is None:
                raise ValueError("leg_group_id required for ARB orders")
            if self.leg_number is None:
                raise ValueError("leg_number required for ARB orders")
            if self.leg_number == ArbLeg.LEG_1 and self.min_fill_ratio is None:
                raise ValueError("min_fill_ratio required on ARB leg 1")
            if self.leg_number == ArbLeg.LEG_2 and self.min_fill_ratio is not None:
                raise ValueError("min_fill_ratio must be None on ARB leg 2")

    @property
    def is_arb(self) -> bool:
        return self.strategy_id == StrategyId.ARB

    @property
    def is_mm(self) -> bool:
        return self.strategy_id in (StrategyId.MM, StrategyId.HEDGE)

    def model_copy(self, update: dict = None) -> "OrderProposal":
        d = asdict(self)
        if update:
            d.update(update)
        return OrderProposal(**d)

    def model_dump(self) -> dict:
        return _model_dump(self)


# ─────────────────────────────────────────────────────────────────────────────
# OrderSubmission — what OM sends to ExecutionEngine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderSubmission:
    order_id:       str
    proposal_id:    str       # == client_order_id at exchange
    market_id:      str
    platform:       Platform
    side:           Side
    size_usdc:      float
    limit_price:    float
    order_type:     OrderType
    strategy_id:    StrategyId
    expiry_ms:      int
    token_quantity: float     # pre-computed: size_usdc / limit_price
    submitted_at:   int
    leg_group_id:   Optional[str]    = None
    leg_number:     Optional[ArbLeg] = None
    min_fill_ratio: Optional[float]  = None

    def __post_init__(self):
        if self.token_quantity <= 0:
            raise ValueError(f"token_quantity must be > 0, got {self.token_quantity}")
        # Validate consistency (allow 0.2% tolerance for rounding)
        expected = self.size_usdc / self.limit_price
        if abs(self.token_quantity - expected) / expected > 0.002:
            raise ValueError(
                f"token_quantity {self.token_quantity:.6f} inconsistent with "
                f"size_usdc/limit_price = {expected:.6f}"
            )

    def model_dump(self) -> dict:
        return _model_dump(self)


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionResult — emitted for every order state change
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionResult:
    proposal_id:       str
    exchange_order_id: str
    status:            OrderStatus
    ts:                int
    filled_size_usdc:  float = 0.0
    fill_price:        Optional[float] = None
    fill_ratio:        Optional[float] = None
    slippage_bps:      Optional[int]   = None
    latency_ms:        int = 0
    tx_hash:           Optional[str]   = None
    exchange_error:    Optional[str]   = None

    def __post_init__(self):
        if self.status in (OrderStatus.PARTIAL, OrderStatus.FILLED):
            if self.fill_price is None:
                raise ValueError(f"fill_price required when status={self.status.value}")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def model_dump(self) -> dict:
        return _model_dump(self)