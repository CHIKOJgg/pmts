"""execution/order_tracker.py — Per-order state machine with fill accumulation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from execution.models import ExecutionResult, OrderSubmission
from src.clock import Clock, LiveClock
from src.types import OrderStatus

FILL_COMPLETE_THRESHOLD: float = 0.999  # dust tolerance
DUST_FLOOR_USDC: float = 0.001


@dataclass(frozen=True)
class FillEvent:
    fill_usdc: float
    fill_price: float
    fill_tokens: float
    ts: int


class TrackerStatus(str, Enum):
    AWAITING = "awaiting"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TrackerStatus.FILLED,
            TrackerStatus.CANCELLED,
            TrackerStatus.EXPIRED,
            TrackerStatus.REJECTED,
            TrackerStatus.TIMEOUT,
        }

    @property
    def is_open(self) -> bool:
        return self in {TrackerStatus.SUBMITTED, TrackerStatus.PARTIAL}


class OrderTracker:
    """
    Tracks a single order from queue → terminal state.

    Accumulates partial fills with weighted-average price.
    All methods are synchronous O(1). Not thread-safe — single asyncio loop only.
    """

    __slots__ = (
        "submission",
        "status",
        "exchange_order_id",
        "fills",
        "created_at",
        "submitted_at",
        "last_poll_at",
        "tx_hash",
        "_clock",
        "_last_latency_ms",
        "nonce",
        "terminal_at",
    )

    def __init__(
        self,
        submission: OrderSubmission,
        clock: Clock = LiveClock(),
    ) -> None:
        self.submission: OrderSubmission = submission
        self.status: TrackerStatus = TrackerStatus.AWAITING
        self.exchange_order_id: Optional[str] = None
        self.fills: List[FillEvent] = []
        self._clock: Clock = clock
        self.created_at: int = self._clock.now_ms()
        self.submitted_at: Optional[int] = None
        self.last_poll_at: Optional[int] = None
        self.tx_hash: Optional[str] = None
        self._last_latency_ms: int = 0
        self.terminal_at: Optional[int] = None
        # Microsecond resolution to prevent nonce collisions (Issue #1)
        self.nonce: int = int(self._clock.now_ms() * 1000)

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def proposal_id(self) -> str:
        return self.submission.proposal_id

    @property
    def cumulative_filled_usdc(self) -> float:
        return sum(f.fill_usdc for f in self.fills)

    @property
    def cumulative_filled_tokens(self) -> float:
        return sum(f.fill_tokens for f in self.fills)

    @property
    def remaining_usdc(self) -> float:
        return max(0.0, self.submission.size_usdc - self.cumulative_filled_usdc)

    @property
    def remaining_tokens(self) -> float:
        return max(0.0, self.submission.token_quantity - self.cumulative_filled_tokens)

    @property
    def fill_ratio(self) -> float:
        size = self.submission.size_usdc
        return min(1.0, self.cumulative_filled_usdc / size) if size > 0 else 0.0

    @property
    def weighted_avg_price(self) -> Optional[float]:
        total = self.cumulative_filled_usdc
        if total <= 0:
            return None
        return sum(f.fill_usdc * f.fill_price for f in self.fills) / total

    @property
    def slippage_bps(self) -> Optional[int]:
        avg = self.weighted_avg_price
        limit = self.submission.limit_price
        if avg is None or limit <= 0:
            return None
        return int(round(abs(avg - limit) / limit * 10_000))

    def is_expired(self, now: int) -> bool:
        return not self.status.is_terminal and now >= self.submission.expiry_ms

    def needs_fast_poll(self, now: int) -> bool:
        if not self.status.is_open:
            return False
        return (self.submission.expiry_ms - now) <= 10_000

    # ── State transitions ─────────────────────────────────────────────────────

    def record_submission(self, exchange_order_id: str) -> ExecutionResult:
        assert self.status == TrackerStatus.AWAITING, f"Expected AWAITING, got {self.status}"
        self.exchange_order_id = exchange_order_id
        self.submitted_at = self._clock.now_ms()
        self.status = TrackerStatus.SUBMITTED
        self._last_latency_ms = self.submitted_at - self.submission.submitted_at
        return self._build(OrderStatus.SUBMITTED, 0.0, None, None, None)

    def record_fill(
        self,
        fill_usdc: float,
        fill_price: float,
        fill_tokens: float,
        ts: int,
    ) -> ExecutionResult:
        assert self.status.is_open or self.status == TrackerStatus.AWAITING, (
            f"record_fill called in terminal state {self.status}"
        )

        self.fills.append(FillEvent(fill_usdc, fill_price, fill_tokens, ts))
        self._last_latency_ms = ts - self.submission.submitted_at

        if self.fill_ratio >= FILL_COMPLETE_THRESHOLD:
            self.status = TrackerStatus.FILLED
            self.terminal_at = self._clock.now_ms()
            final_status = OrderStatus.FILLED
        else:
            self.status = TrackerStatus.PARTIAL
            final_status = OrderStatus.PARTIAL

        return self._build(
            final_status,
            fill_usdc,
            self.weighted_avg_price,
            self.fill_ratio,
            self.slippage_bps,
        )

    def record_cancellation(self) -> ExecutionResult:
        assert not self.status.is_terminal, f"Already terminal: {self.status}"
        self.status = TrackerStatus.CANCELLED
        self.terminal_at = self._clock.now_ms()
        return self._terminal(OrderStatus.CANCELLED)

    def record_expiry(self) -> ExecutionResult:
        assert not self.status.is_terminal, f"Already terminal: {self.status}"
        self.status = TrackerStatus.EXPIRED
        self.terminal_at = self._clock.now_ms()
        return self._terminal(OrderStatus.EXPIRED)

    def record_rejection(self, error: str) -> ExecutionResult:
        assert not self.status.is_terminal, f"Already terminal: {self.status}"
        self.status = TrackerStatus.REJECTED
        self.terminal_at = self._clock.now_ms()
        return ExecutionResult(
            proposal_id=self.proposal_id,
            exchange_order_id=self.exchange_order_id or "unknown",
            status=OrderStatus.REJECTED,
            filled_size_usdc=0.0,
            fill_price=None,
            fill_ratio=None,
            slippage_bps=None,
            latency_ms=self._last_latency_ms,
            tx_hash=None,
            ts=self._clock.now_ms(),
            exchange_error=error,
        )

    def record_timeout(self) -> ExecutionResult:
        assert not self.status.is_terminal, f"Already terminal: {self.status}"
        self.status = TrackerStatus.TIMEOUT
        self.terminal_at = self._clock.now_ms()
        return ExecutionResult(
            proposal_id=self.proposal_id,
            exchange_order_id="unknown",
            status=OrderStatus.TIMEOUT,
            filled_size_usdc=0.0,
            fill_price=None,
            fill_ratio=None,
            slippage_bps=None,
            latency_ms=self._clock.now_ms() - self.submission.submitted_at,
            tx_hash=None,
            ts=self._clock.now_ms(),
            exchange_error=None,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build(
        self,
        status: OrderStatus,
        filled: float,
        fill_price: Optional[float],
        fill_ratio: Optional[float],
        slippage: Optional[int],
    ) -> ExecutionResult:
        return ExecutionResult(
            proposal_id=self.proposal_id,
            exchange_order_id=self.exchange_order_id or "",
            status=status,
            filled_size_usdc=filled,
            fill_price=fill_price,
            fill_ratio=fill_ratio,
            slippage_bps=slippage,
            latency_ms=self._last_latency_ms,
            tx_hash=self.tx_hash,
            ts=self._clock.now_ms(),
            exchange_error=None,
        )

    def _terminal(self, status: OrderStatus) -> ExecutionResult:
        return ExecutionResult(
            proposal_id=self.proposal_id,
            exchange_order_id=self.exchange_order_id or "unknown",
            status=status,
            filled_size_usdc=0.0,  # OM has accumulated from prior PARTIAL results
            fill_price=None,
            fill_ratio=None,
            slippage_bps=None,
            latency_ms=self._clock.now_ms() - self.submission.submitted_at,
            tx_hash=self.tx_hash,
            ts=self._clock.now_ms(),
            exchange_error=None,
        )
