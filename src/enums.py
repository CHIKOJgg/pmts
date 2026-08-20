"""src/types.py — Shared enums and type aliases. Zero external dependencies."""
from __future__ import annotations

from enum import Enum
from typing import NewType

# ── Monetary / numeric type aliases ────────────────────────────────────────────
Usdc         = NewType("Usdc",         float)
PositiveUsdc = NewType("PositiveUsdc", float)
ProbPrice    = NewType("ProbPrice",    float)   # [0.001, 0.999]
FillRatio    = NewType("FillRatio",    float)   # [0.0, 1.0]
DrawdownFrac = NewType("DrawdownFrac", float)
BasisPoints  = NewType("BasisPoints",  int)
OFI          = NewType("OFI",          float)   # [-1.0, 1.0]
Days         = NewType("Days",         float)
UuidStr      = NewType("UuidStr",      str)
EpochMs      = NewType("EpochMs",      int)


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    OPINION    = "opinion"


class Side(str, Enum):
    BUY_YES  = "buy_yes"
    BUY_NO   = "buy_no"
    SELL_YES = "sell_yes"
    SELL_NO  = "sell_no"

    @property
    def is_buy(self) -> bool:
        return self in (Side.BUY_YES, Side.BUY_NO)

    @property
    def is_yes(self) -> bool:
        return self in (Side.BUY_YES, Side.SELL_YES)


class OrderType(str, Enum):
    LIMIT = "limit"


class OrderStatus(str, Enum):
    SUBMITTED = "submitted"
    PARTIAL   = "partial"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    EXPIRED   = "expired"
    REJECTED  = "rejected"
    TIMEOUT   = "timeout"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.TIMEOUT,
        }

    @property
    def is_open(self) -> bool:
        return self in {OrderStatus.SUBMITTED, OrderStatus.PARTIAL}


class StrategyId(str, Enum):
    ARB   = "arb"
    MM    = "mm"
    HEDGE = "hedge"


class ArbLeg(int, Enum):
    LEG_1 = 1
    LEG_2 = 2


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RejectReason(str, Enum):
    KILL_SWITCH_ACTIVE    = "kill_switch_active"
    CONNECTOR_DOWN        = "connector_down"
    DRAWDOWN_LIMIT        = "drawdown_limit"
    DUPLICATE_PROPOSAL    = "duplicate_proposal"
    ORDER_TOO_SMALL       = "order_too_small"
    ORDER_TOO_LARGE       = "order_too_large"
    LIQUIDITY_BUFFER      = "liquidity_buffer"
    INSUFFICIENT_CAPITAL  = "insufficient_capital"
    MARKET_EXPOSURE_LIMIT = "market_exposure_limit"
    STRATEGY_CAP_EXCEEDED = "strategy_cap_exceeded"
    DELTA_LIMIT           = "delta_limit"
    STALE_MTM             = "stale_mtm"
    SESSION_LOSS_LIMIT    = "session_loss_limit"
    SOFT_KILL_ACTIVE      = "soft_kill_active"


class ConnectorStatus(str, Enum):
    UP       = "up"
    DEGRADED = "degraded"
    DOWN     = "down"


class Outcome(str, Enum):
    YES = "yes"
    NO  = "no"
