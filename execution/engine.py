"""execution/engine.py — Order submission, fill tracking, and expiry enforcement."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field as dc_field
from typing import Callable, Coroutine, List, Optional, Protocol, runtime_checkable

from execution.models import ExecutionResult, OrderSubmission
from execution.order_tracker import OrderTracker, TrackerStatus
from src.errors import ExchangeRejected
from src.types import OrderStatus, Platform, Side, StrategyId, OrderType, ArbLeg
from infrastructure.observability import FILLS_TOTAL, FILL_USDC_TOTAL, ORDER_LATENCY, API_ERRORS_TOTAL
import json

logger = logging.getLogger(__name__)

_ResultCB = Callable[[ExecutionResult], Coroutine]

# Retry policy
MAX_SUBMIT_ATTEMPTS: int   = 3
SUBMIT_BASE_DELAY_S: float = 0.200
TICK_SIZE:           float = 0.001   # 1 probability tick

# Poll / expiry intervals — overridable per-instance in tests
DEFAULT_POLL_NORMAL_S:  float = 2.0
DEFAULT_POLL_FAST_S:    float = 0.5
DEFAULT_EXPIRY_CHECK_S: float = 0.25


# ─────────────────────────────────────────────────────────────────────────────
# Exchange response dataclasses (defined BEFORE ExchangeClient protocol)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlacedFill:
    fill_usdc:   float
    fill_price:  float
    fill_tokens: float
    ts:          int


@dataclass
class PlacedOrderResponse:
    exchange_order_id: str
    status:            str            # "live" | "matched" | "cancelled"
    fills:             List[PlacedFill] = dc_field(default_factory=list)
    tx_hash:           Optional[str]    = None


@dataclass
class OrderStatusFill:
    fill_usdc:   float
    fill_price:  float
    fill_tokens: float
    ts:          int


@dataclass
class OrderStatusResponse:
    exchange_order_id: str
    is_live:           bool
    is_cancelled:      bool
    is_filled:         bool
    remaining_usdc:    float
    new_fills:         List[OrderStatusFill] = dc_field(default_factory=list)
    tx_hash:           Optional[str]         = None


@dataclass
class OpenOrder:
    exchange_order_id: str
    market_id:         str
    side:              str
    size_usdc:         float
    filled_usdc:       float
    limit_price:       float
    ts:                int


# ─────────────────────────────────────────────────────────────────────────────
# ExchangeClient Protocol
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class ExchangeClient(Protocol):
    """Interface that concrete exchange clients must satisfy."""

    @property
    def platform(self) -> Platform: ...

    async def place_order(
        self, submission: OrderSubmission, effective_price: float, nonce: Optional[int] = None
    ) -> PlacedOrderResponse: ...

    async def cancel_order(self, exchange_order_id: str, market_id: str) -> bool: ...

    async def get_order_status(
        self, exchange_order_id: str, market_id: str
    ) -> OrderStatusResponse: ...

    async def verify_connectivity(self) -> bool:
        """
        Perform a minimal authenticated request to verify API credentials
        and connectivity to the exchange (supports sandbox/testnet).
        """
        ...

    async def get_open_orders(self, market_ids: Optional[List[str]] = None) -> List[OpenOrder]:
        """
        Fetch all open orders for the account, optionally filtered by market.
        Used for startup reconciliation.
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Priority queue entry
# ─────────────────────────────────────────────────────────────────────────────

class _QueueEntry:
    __slots__ = ("priority", "seq", "submission")

    def __init__(self, priority: int, seq: int, submission: OrderSubmission) -> None:
        self.priority   = priority
        self.seq        = seq
        self.submission = submission

    def __lt__(self, other: "_QueueEntry") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)

    def __le__(self, other: "_QueueEntry") -> bool:
        return (self.priority, self.seq) <= (other.priority, other.seq)


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionEngine
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Manages the full order lifecycle for one exchange venue.

    Three background asyncio tasks:
      _submit_worker — priority queue (ARB=0 before MM/HEDGE=1), retry 5xx
      _poll_worker   — adaptive interval (2s normal, 0.5s near-expiry)
      _expiry_worker — 250ms tick, cancels expired orders

    Instance-level timing attributes allow test overrides without touching globals.
    """

    def __init__(
        self,
        client:         ExchangeClient,
        risk:           Optional[Any] = None, # RiskEngine
        store:          Optional[Any] = None, # SqlitePortfolioStore
        mdb:            Optional[Any] = None, # MarketDataProvider
        max_concurrent: int = 5,
    ) -> None:
        self._client    = client
        self._risk      = risk
        self._store     = store
        self._mdb       = mdb

        # Per-order trackers
        self._trackers:         dict[str, OrderTracker] = {}
        self._exch_to_proposal: dict[str, str]          = {}

        # Priority submission queue
        self._queue:     asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq:       int                   = 0
        self._semaphore: asyncio.Semaphore     = asyncio.Semaphore(max_concurrent)

        # Timing — overridable in tests
        self.poll_normal_s    = DEFAULT_POLL_NORMAL_S
        self.poll_fast_s      = DEFAULT_POLL_FAST_S
        self.expiry_check_s   = DEFAULT_EXPIRY_CHECK_S
        self.submit_base_delay_s = SUBMIT_BASE_DELAY_S

        self._callbacks: list[_ResultCB] = []
        self._tasks:     list[asyncio.Task] = []
        self._stopped:   bool = False

        # Metrics
        self.orders_submitted:  int   = 0
        self.orders_filled:     int   = 0
        self.orders_partial:    int   = 0
        self.orders_cancelled:  int   = 0
        self.orders_expired:    int   = 0
        self.orders_rejected:   int   = 0
        self.orders_timed_out:  int   = 0
        self.total_filled_usdc: float = 0.0
        self.submit_retries:    int   = 0
        self.reconciliation_complete: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopped = False
        n = max(1, self._semaphore._value)
        for i in range(n):
            self._tasks.append(asyncio.create_task(
                self._submit_worker(i),
                name=f"exec-submit-{self._client.platform.value}-{i}",
            ))
        self._tasks.append(asyncio.create_task(
            self._poll_worker(),
            name=f"exec-poll-{self._client.platform.value}",
        ))
        self._tasks.append(asyncio.create_task(
            self._expiry_worker(),
            name=f"exec-expiry-{self._client.platform.value}",
        ))
        logger.info("ExecutionEngine started (%s)", self._client.platform.value)

    async def stop(self) -> None:
        self._stopped = True
        logger.info("ExecutionEngine stopping (%s). Draining queue...", self._client.platform.value)
        
        # Step 5: Wait for queue to drain before cancelling workers (Issue #5)
        while not self._queue.empty():
            await asyncio.sleep(0.1)
            
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("ExecutionEngine stopped (%s)", self._client.platform.value)

    async def reconcile(self) -> None:
        """
        Reconstruct state from exchange and persistent store (Step 6).
        Must be called before the engine is marked as ready.
        """
        if not self._store:
            logger.warning("No store provided, skipping reconciliation for %s", self._client.platform.value)
            return

        logger.info("Starting reconciliation for %s...", self._client.platform.value)
        
        # 1. Load active orders from DB
        db_orders = self._store.load_active_orders()
        db_map = {proposal_id: (exch_id, sub_json) for proposal_id, exch_id, sub_json in db_orders}
        
        # 2. Query exchange for open orders
        try:
            exch_orders = await self._client.get_open_orders()
            exch_map = {o.exchange_order_id: o for o in exch_orders}
        except Exception as exc:
            logger.error("Failed to fetch open orders from %s: %s", self._client.platform.value, exc)
            return

        # 3. Reconcile
        for proposal_id, (exch_id, sub_json) in db_map.items():
            try:
                # Deserialize submission
                sub_dict = json.loads(sub_json)
                # Helper to reconstruct Enums
                sub_dict['platform'] = Platform(sub_dict['platform'])
                sub_dict['side'] = Side(sub_dict['side'])
                sub_dict['order_type'] = OrderType(sub_dict['order_type'])
                sub_dict['strategy_id'] = StrategyId(sub_dict['strategy_id'])
                if sub_dict.get('leg_number'): sub_dict['leg_number'] = ArbLeg(sub_dict['leg_number'])
                
                submission = OrderSubmission(**sub_dict)
                tracker = OrderTracker(submission)
                
                if exch_id and exch_id in exch_map:
                    # Order is still live on exchange
                    o = exch_map[exch_id]
                    logger.info("Re-found live order %s (proposal %s), filled=%.2f/%.2f", 
                                exch_id, proposal_id[:8], o.filled_usdc, o.size_usdc)
                    
                    tracker.exchange_order_id = exch_id
                    tracker.status = TrackerStatus.SUBMITTED
                    
                    if o.filled_usdc > 0:
                        # Record a synthetic fill to account for what happened while we were down
                        tracker.record_fill(
                            o.filled_usdc, o.limit_price, 
                            o.filled_usdc / o.limit_price if o.limit_price > 0 else 0,
                            o.ts
                        )
                    
                    self._trackers[proposal_id] = tracker
                    self._exch_to_proposal[exch_id] = proposal_id

                    if tracker.status.is_terminal:
                        self._finalise(tracker)
                else:
                    # Order not on exchange, maybe terminal while we were down
                    logger.info("Order %s (proposal %s) not on exchange, checking final status...", exch_id or "N/A", proposal_id[:8])
                    if exch_id:
                        tracker.exchange_order_id = exch_id
                        tracker.status = TrackerStatus.SUBMITTED
                        await self._poll_one(tracker)
                    else:
                        # Never submitted or failed during submission
                        result = tracker.record_rejection("Lost during crash")
                        self._finalise(tracker)
                        await self._dispatch(result)
            except Exception as exc:
                logger.error("Reconciliation failed for proposal %s: %s", proposal_id, exc, exc_info=True)

        self.reconciliation_complete = True
        logger.info("Reconciliation complete for %s. Trackers: %d", self._client.platform.value, len(self._trackers))

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit(self, submission: OrderSubmission) -> None:
        priority = 0 if submission.strategy_id == StrategyId.ARB else 1
        self._seq += 1
        self._trackers[submission.proposal_id] = OrderTracker(submission)
        
        if self._store:
            # Step 6: Persist submission before trying to send it
            self._store.save_order(
                submission.proposal_id, 
                json.dumps(submission.model_dump())
            )

        await self._queue.put(_QueueEntry(priority, self._seq, submission))

    async def cancel(self, proposal_id: str) -> None:
        """Cancel by proposal_id. Safe to call on unknown or terminal orders."""
        tracker = self._trackers.get(proposal_id)
        if tracker is None or tracker.status.is_terminal:
            return

        if tracker.status == TrackerStatus.AWAITING:
            result = tracker.record_cancellation()
            self.orders_cancelled += 1
            self._finalise(tracker)
            await self._dispatch(result)
            return

        if tracker.exchange_order_id is None:
            result = tracker.record_cancellation()
            self.orders_cancelled += 1
            self._finalise(tracker)
            await self._dispatch(result)
            return

        try:
            confirmed = await self._client.cancel_order(
                tracker.exchange_order_id,
                tracker.submission.market_id,
            )
        except Exception as exc:
            logger.warning("Cancel failed for %s: %s", proposal_id[:8], exc)
            return

        if confirmed:
            result = tracker.record_cancellation()
            self.orders_cancelled += 1
            self._finalise(tracker)
            await self._dispatch(result)
        else:
            await self._poll_one(tracker)

    def add_result_callback(self, cb: _ResultCB) -> None:
        self._callbacks.append(cb)

    def get_tracker(self, proposal_id: str) -> Optional[OrderTracker]:
        return self._trackers.get(proposal_id)

    @property
    def live_order_count(self) -> int:
        return sum(1 for t in self._trackers.values() if t.status.is_open)

    # ── Submit worker ─────────────────────────────────────────────────────────

    async def _submit_worker(self, worker_id: int) -> None:
        while not self._stopped:
            try:
                entry: _QueueEntry = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

            tracker = self._trackers.get(entry.submission.proposal_id)
            if tracker is None or tracker.status.is_terminal:
                self._queue.task_done()
                continue

            async with self._semaphore:
                await self._execute_submission(tracker)
            self._queue.task_done()

    async def _execute_submission(self, tracker: OrderTracker) -> None:
        submission = tracker.submission
        now        = _now_ms()

        if tracker.is_expired(now):
            result = tracker.record_expiry()
            self.orders_expired += 1
            await self._dispatch(result)
            return

        effective_price = self._effective_price(submission, now)

        for attempt in range(MAX_SUBMIT_ATTEMPTS):
            if tracker.status.is_terminal:
                return
            try:
                placed = await self._client.place_order(submission, effective_price, nonce=tracker.nonce)
                break
            except ExchangeRejected as exc:
                result = tracker.record_rejection(str(exc))
                API_ERRORS_TOTAL.labels(platform=self._client.platform.value, error_type="rejected").inc()
                self.orders_rejected += 1
                self._finalise(tracker)
                await self._dispatch(result)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt < MAX_SUBMIT_ATTEMPTS - 1:
                    self.submit_retries += 1
                    delay = self.submit_base_delay_s * (2 ** attempt)
                    logger.warning(
                        "Submit attempt %d/%d failed for %s: %s — retry in %.1fs",
                        attempt + 1, MAX_SUBMIT_ATTEMPTS,
                        submission.proposal_id[:8], exc, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    result = tracker.record_timeout()
                    API_ERRORS_TOTAL.labels(platform=self._client.platform.value, error_type="timeout").inc()
                    self.orders_timed_out += 1
                    self._finalise(tracker)
                    await self._dispatch(result)
                    return
        else:
            return

        # Register ACK
        submit_result = tracker.record_submission(placed.exchange_order_id)
        self._exch_to_proposal[placed.exchange_order_id] = submission.proposal_id
        
        if self._store:
            # Step 6: Update with exchange_order_id
            self._store.update_order_exchange_id(submission.proposal_id, placed.exchange_order_id)

        self.orders_submitted += 1
        await self._dispatch(submit_result)

        if placed.tx_hash:
            tracker.tx_hash = placed.tx_hash

        # Process any immediate fills from placement response
        for fill in placed.fills:
            if tracker.status.is_terminal:
                break
            result = tracker.record_fill(
                fill.fill_usdc, fill.fill_price, fill.fill_tokens, fill.ts
            )
            FILLS_TOTAL.labels(platform=self._client.platform.value, strategy=tracker.submission.strategy_id.value).inc()
            FILL_USDC_TOTAL.labels(platform=self._client.platform.value).inc(fill.fill_usdc)
            ORDER_LATENCY.labels(platform=self._client.platform.value).observe(result.latency_ms / 1000.0)
            
            self.total_filled_usdc += fill.fill_usdc
            if result.status == OrderStatus.FILLED:
                self.orders_filled += 1
            else:
                self.orders_partial += 1
            await self._dispatch(result)

        if tracker.status == TrackerStatus.FILLED:
            self._finalise(tracker)

    # ── Poll worker ───────────────────────────────────────────────────────────

    async def _poll_worker(self) -> None:
        while not self._stopped:
            now  = _now_ms()
            live = [t for t in self._trackers.values() if t.status.is_open]
            
            if not live:
                try:
                    await asyncio.sleep(self.poll_normal_s)
                except asyncio.CancelledError:
                    return
                continue

            any_urgent = any(t.needs_fast_poll(now) for t in live)
            interval = self.poll_fast_s if any_urgent else self.poll_normal_s
            
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

            if self._stopped:
                break

            for tracker in live:
                if self._stopped or tracker.status.is_terminal:
                    continue
                had_fill = await self._poll_one(tracker)
                if had_fill and not tracker.status.is_terminal:
                    await asyncio.sleep(0.1)    # burst: re-poll quickly
                    await self._poll_one(tracker)

    async def _poll_one(self, tracker: OrderTracker) -> bool:
        if tracker.exchange_order_id is None:
            return False
        tracker.last_poll_at = _now_ms()

        try:
            resp = await self._client.get_order_status(
                tracker.exchange_order_id,
                tracker.submission.market_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Poll error for %s: %s", tracker.proposal_id[:8], exc)
            return False

        if resp.tx_hash and not tracker.tx_hash:
            tracker.tx_hash = resp.tx_hash

        had_fill = False

        for fill in resp.new_fills:
            if tracker.status.is_terminal:
                break
            result = tracker.record_fill(
                fill.fill_usdc, fill.fill_price, fill.fill_tokens, fill.ts
            )
            FILLS_TOTAL.labels(platform=self._client.platform.value, strategy=tracker.submission.strategy_id.value).inc()
            FILL_USDC_TOTAL.labels(platform=self._client.platform.value).inc(fill.fill_usdc)
            ORDER_LATENCY.labels(platform=self._client.platform.value).observe(result.latency_ms / 1000.0)
            
            self.total_filled_usdc += fill.fill_usdc
            if result.status == OrderStatus.FILLED:
                self.orders_filled += 1
            else:
                self.orders_partial += 1
            await self._dispatch(result)
            had_fill = True

        if not tracker.status.is_terminal:
            if resp.is_filled and not had_fill:
                remaining = tracker.remaining_usdc
                if remaining > DUST_FLOOR_USDC_LOCAL:
                    result = tracker.record_fill(
                        remaining,
                        tracker.submission.limit_price,
                        tracker.remaining_tokens,
                        _now_ms(),
                    )
                    self.total_filled_usdc += remaining
                    self.orders_filled += 1
                    await self._dispatch(result)
                    had_fill = True
            elif resp.is_cancelled and not tracker.status.is_terminal:
                result = tracker.record_cancellation()
                self.orders_cancelled += 1
                await self._dispatch(result)

        if tracker.status.is_terminal:
            self._finalise(tracker)
        return had_fill

    # ── Expiry worker ─────────────────────────────────────────────────────────

    async def _expiry_worker(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self.expiry_check_s)
            except asyncio.CancelledError:
                return

            now = _now_ms()
            for tracker in list(self._trackers.values()):
                if tracker.status.is_terminal or not tracker.is_expired(now):
                    continue
                logger.info(
                    "Order expired: %s (filled=%.2f/%.2f USDC)",
                    tracker.proposal_id[:8],
                    tracker.cumulative_filled_usdc,
                    tracker.submission.size_usdc,
                )
                if tracker.exchange_order_id:
                    try:
                        await self._client.cancel_order(
                            tracker.exchange_order_id,
                            tracker.submission.market_id,
                        )
                    except Exception as exc:
                        logger.warning("Expiry cancel failed: %s", exc)
                result = tracker.record_expiry()
                self.orders_expired += 1
                self._finalise(tracker)
                await self._dispatch(result)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _effective_price(self, submission: OrderSubmission, now: int) -> float:
        """
        ARB orders near expiry cross spread by 1 tick to improve fill odds.
        MM/HEDGE orders always post at the strategy's intended price.
        """
        if submission.strategy_id != StrategyId.ARB:
            return submission.limit_price

        total_window = submission.expiry_ms - submission.submitted_at
        if total_window <= 0:
            return submission.limit_price

        urgency = (now - submission.submitted_at) / total_window
        if urgency < 0.80:
            return submission.limit_price

        if self._mdb is None:
            return submission.limit_price

        snap = self._mdb.get_snapshot(submission.market_id, submission.platform)
        if snap is None:
            return submission.limit_price

        if submission.side.is_buy:
            best_ask = snap.yes_ask if submission.side.is_yes else snap.no_ask
            aggressive = round(best_ask + TICK_SIZE, 4)
            return min(aggressive, submission.limit_price)
        else:
            best_bid = snap.yes_bid if submission.side.is_yes else snap.no_bid
            aggressive = round(best_bid - TICK_SIZE, 4)
            return max(aggressive, submission.limit_price)

    def _finalise(self, tracker: OrderTracker) -> None:
        if tracker.exchange_order_id:
            self._exch_to_proposal.pop(tracker.exchange_order_id, None)
        # Remove terminal tracker to prevent unbounded memory growth
        self._trackers.pop(tracker.proposal_id, None)
        
        if self._store:
            # Step 6: Remove from active orders in DB
            self._store.remove_order(tracker.proposal_id)


    async def _dispatch(self, result: ExecutionResult) -> None:
        for cb in self._callbacks:
            try:
                await cb(result)
            except Exception as exc:
                logger.error("ExecutionResult callback raised: %s", exc, exc_info=True)


DUST_FLOOR_USDC_LOCAL: float = 0.001


def _now_ms() -> int:
    return int(time.time() * 1000)