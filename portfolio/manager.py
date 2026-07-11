"""portfolio/manager.py — Position tracking, cost basis, MTM, and P&L."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from infrastructure.observability import (
    CAPITAL_UTILIZATION,
    OPEN_EXPOSURE_USDC,
    PORTFOLIO_MTM_USDC,
    PORTFOLIO_REALISED_PNL_USDC,
    STRATEGY_FILL_USDC_TOTAL,
)
from src.clock import Clock, LiveClock
from src.enums import Outcome, Platform, Side
from src.errors import NegativeHoldings

logger = logging.getLogger(__name__)

DUST_FLOOR: float = 1e-9
SNAPSHOT_INTERVAL_S: float = 60.0

_PriceSource = Callable[[str, Platform], Optional[Tuple[float, float]]]


# ─────────────────────────────────────────────────────────────────────────────
# Public DTOs returned by PortfolioManager
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FillRecord:
    proposal_id: str
    order_id: str
    market_id: str
    platform: Platform
    side: str  # Side.value
    filled_usdc: float
    fill_price: float
    ts: int
    strategy_id: Optional[str] = None
    realised_pnl: float = 0.0
    hold_time_ms: float = 0.0


@dataclass(frozen=True)
class RedemptionRecord:
    market_id: str
    platform: Platform
    outcome: Outcome
    usdc_received: float
    ts: int


@dataclass(frozen=True)
class DeltaResult:
    market_id: str
    net_delta: float
    yes_holdings_pm: float
    no_holdings_pm: float
    yes_holdings_op: float
    no_holdings_op: float
    avg_cost_yes_pm: Optional[float] = None
    avg_cost_no_pm: Optional[float] = None
    avg_cost_yes_op: Optional[float] = None
    avg_cost_no_op: Optional[float] = None


@dataclass(frozen=True)
class PortfolioMTM:
    total_cash_usdc: float
    total_positions_mtm: float
    total_equity_usdc: float
    ts: int


# ─────────────────────────────────────────────────────────────────────────────
# Internal per-(market, platform) ledger
# ─────────────────────────────────────────────────────────────────────────────


class _Position:
    """
    Mutable per-(market_id, platform) position ledger.
    Uses weighted-average cost basis (not FIFO).
    NOT thread-safe — must be accessed under PortfolioManager._lock.
    """

    __slots__ = (
        "market_id",
        "platform",
        "yes_qty",
        "no_qty",
        "avg_cost_yes",
        "avg_cost_no",
        "realised_pnl",
        "last_price_ts",
    )

    def __init__(self, market_id: str, platform: Platform) -> None:
        self.market_id: str = market_id
        self.platform: Platform = platform
        self.yes_qty: float = 0.0
        self.no_qty: float = 0.0
        self.avg_cost_yes: Optional[float] = None
        self.avg_cost_no: Optional[float] = None
        self.realised_pnl: float = 0.0
        self.last_price_ts: int = 0

    @property
    def net_delta(self) -> float:
        return self.yes_qty - self.no_qty

    @property
    def is_flat(self) -> bool:
        return self.yes_qty < DUST_FLOOR and self.no_qty < DUST_FLOOR

    def apply_fill(self, side: Side, tokens: float, price: float) -> None:
        if side == Side.BUY_YES:
            self.yes_qty, self.avg_cost_yes = _wavg(self.yes_qty, self.avg_cost_yes, tokens, price)
        elif side == Side.BUY_NO:
            self.no_qty, self.avg_cost_no = _wavg(self.no_qty, self.avg_cost_no, tokens, price)
        elif side == Side.SELL_YES:
            if tokens > self.yes_qty + DUST_FLOOR:
                raise NegativeHoldings(
                    f"SELL_YES {tokens:.6f} > holdings {self.yes_qty:.6f}",
                    market_id=self.market_id,
                    platform=self.platform.value,
                    token_side="yes",
                    current_holdings=self.yes_qty,
                    fill_size=tokens,
                )
            if self.avg_cost_yes is not None:
                self.realised_pnl += tokens * (price - self.avg_cost_yes)
            self.yes_qty = max(0.0, self.yes_qty - tokens)
            if self.yes_qty < DUST_FLOOR:
                self.yes_qty = 0.0
                self.avg_cost_yes = None
        else:  # SELL_NO
            if tokens > self.no_qty + DUST_FLOOR:
                raise NegativeHoldings(
                    f"SELL_NO {tokens:.6f} > holdings {self.no_qty:.6f}",
                    market_id=self.market_id,
                    platform=self.platform.value,
                    token_side="no",
                    current_holdings=self.no_qty,
                    fill_size=tokens,
                )
            if self.avg_cost_no is not None:
                self.realised_pnl += tokens * (price - self.avg_cost_no)
            self.no_qty = max(0.0, self.no_qty - tokens)
            if self.no_qty < DUST_FLOOR:
                self.no_qty = 0.0
                self.avg_cost_no = None

    def mtm(self, yes_mid: float, no_mid: float) -> float:
        return self.yes_qty * yes_mid + self.no_qty * no_mid

    def close_on_redemption(self, outcome: str, usdc_received: float) -> None:
        """Close position on market resolution."""
        if outcome == "yes":
            # YES resolved - receive USDC for each YES token held
            qty = self.yes_qty
            cost = self.avg_cost_yes or 0.0
            self.realised_pnl += usdc_received - cost * qty
            self.yes_qty = 0.0
            self.avg_cost_yes = None
            # NO tokens expire worthless — realise the cost as a loss
            if self.avg_cost_no is not None:
                self.realised_pnl -= self.no_qty * self.avg_cost_no
        elif outcome == "no":
            # NO resolved - receive USDC for each NO token held
            qty = self.no_qty
            cost = self.avg_cost_no or 0.0
            self.realised_pnl += usdc_received - cost * qty
            self.no_qty = 0.0
            self.avg_cost_no = None
            # YES tokens expire worthless — realise the cost as a loss
            if self.avg_cost_yes is not None:
                self.realised_pnl -= self.yes_qty * self.avg_cost_yes
        else:
            logger.warning(f"Unknown resolution outcome: {outcome}")

        # For binary markets, both positions are resolved regardless of which outcome occurred
        if outcome in ("yes", "no"):
            self.yes_qty = 0.0
            self.no_qty = 0.0


def _wavg(
    prev_qty: float,
    prev_avg: Optional[float],
    fill_qty: float,
    fill_price: float,
) -> Tuple[float, Optional[float]]:
    new_qty = prev_qty + fill_qty
    if new_qty < DUST_FLOOR:
        return 0.0, None
    if prev_avg is None or prev_qty < DUST_FLOOR:
        return new_qty, fill_price
    new_avg = (prev_qty * prev_avg + fill_qty * fill_price) / new_qty
    return new_qty, new_avg


# ─────────────────────────────────────────────────────────────────────────────
# PortfolioManager
# ─────────────────────────────────────────────────────────────────────────────


class PortfolioManager:
    """
    In-process position store.

    - All mutations via asyncio.Lock (record_fill, reserve_capital, release_capital)
    - Hot-path reads (get_delta, get_portfolio_mtm) are lock-free synchronous —
      safe because asyncio is single-threaded and dict reads are atomic under GIL.
    - Capital reservations tracked directly here to prevent TOCTOU race in RiskEngine.
    """

    def __init__(
        self,
        initial_cash_usdc: float,
        price_source: _PriceSource,
        stream_writer: Optional[Callable[..., Any]] = None,
        store: Any = None,
        clock: Optional[Clock] = None,
        fill_callback: Optional[Callable[[FillRecord], None]] = None,
    ) -> None:
        if initial_cash_usdc < 0:
            raise ValueError(f"initial_cash_usdc must be ≥ 0, got {initial_cash_usdc}")

        self._lock: asyncio.Lock = asyncio.Lock()
        # Synchronous lock for hot-path reads (get_portfolio_mtm)
        self._sync_lock: threading.Lock = threading.Lock()
        self._positions: Dict[Tuple[str, Platform], _Position] = {}
        self._initial_capital: float = initial_cash_usdc
        self._cash_usdc: float = initial_cash_usdc
        self._reserved_usdc: float = 0.0
        self._peak_equity: float = initial_cash_usdc
        self._closed_pnl: float = 0.0
        self._price_source: _PriceSource = price_source
        self._stream_writer: Optional[Callable[..., Any]] = stream_writer
        self._store = store
        self._clock = clock or LiveClock()
        self._fill_callback = fill_callback

        if self._store:
            state = self._store.load_state()
            if state["cash_usdc"] is not None:
                self._cash_usdc = state["cash_usdc"]
            if state["peak_equity"] is not None:
                self._peak_equity = state["peak_equity"]
            self._closed_pnl = state.get("closed_pnl", 0.0)
            self._positions = state.get("positions", {})
            logger.info(
                "Loaded portfolio state from SQLite. Cash: $%.2f, Positions: %d", self._cash_usdc, len(self._positions)
            )

        self._tasks: list[asyncio.Task[None]] = []
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._stopped: bool = False

        self.fill_count: int = 0
        self.redemption_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopped = False
        self._tasks.append(asyncio.create_task(self._snapshot_loop(), name="portfolio-snapshot"))

    async def stop(self) -> None:
        self._stopped = True
        if not self._tasks:
            return
        # Cancel all tasks safely
        for t in self._tasks:
            t.cancel()
        # Create a new event loop if the current one is closed
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and not loop.is_closed():
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Handle background tasks similarly
        if not self._background_tasks:
            return
        for t in self._background_tasks:
            t.cancel()
        try:
            loop = asyncio.get_running_loop()
            if loop is not None and not loop.is_closed():
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
        except RuntimeError:
            pass  # No running loop
        self._background_tasks.clear()

    # ── Mutations (locked) ────────────────────────────────────────────────────

    async def record_fill(self, fill: FillRecord) -> None:
        if fill.fill_price <= 0:
            logger.warning("record_fill: fill_price is %s — skipping", fill.fill_price)
            return
        tokens = fill.filled_usdc / fill.fill_price
        try:
            side = Side(fill.side.lower())
        except ValueError:
            logger.error("Invalid side in fill record: %s", fill.side)
            return
        key = (fill.market_id, fill.platform)

        async with self._lock:
            if key not in self._positions:
                self._positions[key] = _Position(fill.market_id, fill.platform)
            pos = self._positions[key]
            old_pnl = pos.realised_pnl
            pos.apply_fill(side, tokens, fill.fill_price)
            pnl_delta = pos.realised_pnl - old_pnl

            if side.is_buy:
                self._cash_usdc = max(0.0, self._cash_usdc - fill.filled_usdc)
            else:
                self._cash_usdc += fill.filled_usdc

            equity = self._equity_locked()
            if equity > self._peak_equity:
                self._peak_equity = equity

            self.fill_count += 1

            if fill.strategy_id:
                STRATEGY_FILL_USDC_TOTAL.labels(strategy=fill.strategy_id).inc(fill.filled_usdc)

            # Update exposure metric
            exposure = self.get_market_exposure_usdc(fill.market_id)
            OPEN_EXPOSURE_USDC.labels(market_id=fill.market_id).set(exposure)

            if self._store:
                fill_with_pnl = replace(fill, realised_pnl=pnl_delta)
                self._store.save_fill_and_position(
                    fill_with_pnl, pos, self._cash_usdc, self._peak_equity, self._closed_pnl
                )

            if self._fill_callback:
                self._fill_callback(replace(fill, realised_pnl=pnl_delta))

        task = asyncio.create_task(
            self._publish_snapshot(),
            name=f"snap-{fill.proposal_id[:8]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def record_redemption(self, redemption: RedemptionRecord) -> None:
        key = (redemption.market_id, redemption.platform)
        async with self._lock:
            pos = self._positions.get(key)
            if pos is None:
                logger.warning(
                    "record_redemption: no position for %s/%s", redemption.market_id, redemption.platform.value
                )
                return
            pos.close_on_redemption(redemption.outcome.value, redemption.usdc_received)
            self._cash_usdc += redemption.usdc_received
            self._closed_pnl += pos.realised_pnl
            del self._positions[key]
            equity = self._equity_locked()
            if equity > self._peak_equity:
                self._peak_equity = equity
            self.redemption_count += 1

            # Update exposure metric
            exposure = self.get_market_exposure_usdc(redemption.market_id)
            OPEN_EXPOSURE_USDC.labels(market_id=redemption.market_id).set(exposure)

            if self._store:
                pos_to_save = None if pos.is_flat else pos
                self._store.save_redemption(
                    redemption.market_id,
                    redemption.platform,
                    self._cash_usdc,
                    self._peak_equity,
                    self._closed_pnl,
                    pos_to_save,
                )

    # ── Capital reservation ──────────────────────────────────────────────────

    async def reserve_capital(self, amount: float) -> None:
        async with self._lock:
            self._reserved_usdc += amount

    def reserve_capital_sync(self, amount: float) -> None:
        """Synchronous variant for RiskEngine.evaluate() which runs on the event loop thread."""
        with self._sync_lock:
            self._reserved_usdc += amount

    async def release_capital(self, amount: float) -> None:
        async with self._lock:
            self._reserved_usdc = max(0.0, self._reserved_usdc - amount)

    # ── Hot-path reads (lock-free, synchronous) ───────────────────────────────

    def get_delta(self, market_id: str) -> DeltaResult:
        pm = self._positions.get((market_id, Platform.POLYMARKET))
        op = self._positions.get((market_id, Platform.OPINION))
        y_pm = pm.yes_qty if pm else 0.0
        n_pm = pm.no_qty if pm else 0.0
        y_op = op.yes_qty if op else 0.0
        n_op = op.no_qty if op else 0.0
        return DeltaResult(
            market_id=market_id,
            net_delta=(y_pm + y_op) - (n_pm + n_op),
            yes_holdings_pm=y_pm,
            no_holdings_pm=n_pm,
            yes_holdings_op=y_op,
            no_holdings_op=n_op,
        )

    def get_portfolio_mtm(self) -> PortfolioMTM:
        """Compute mark-to-market equity.

        Takes a deep snapshot of position state under the sync lock to
        prevent inconsistent reads when record_fill mutates positions concurrently.
        """
        # Take atomic snapshot of all mutable state
        with self._sync_lock:
            cash = self._cash_usdc
            reserved = self._reserved_usdc
            closed_pnl = self._closed_pnl
            # Deep-copy position numeric fields to avoid reading stale/mutated state
            pos_snapshots: list[tuple[str, Platform, float, float]] = []
            for (mid, plat), pos in self._positions.items():
                pos_snapshots.append((mid, plat, pos.yes_qty, pos.no_qty))

        total_pos = 0.0
        for mid, plat, yes_qty, no_qty in pos_snapshots:
            prices = self._price_source(mid, plat)
            if prices:
                yes_mid, no_mid = prices
                total_pos += yes_qty * yes_mid + no_qty * no_mid
        equity = cash + total_pos
        total_realised = sum(p.realised_pnl for p in self._positions.values()) + closed_pnl
        PORTFOLIO_MTM_USDC.set(equity)
        PORTFOLIO_REALISED_PNL_USDC.set(total_realised)
        CAPITAL_UTILIZATION.set((reserved / equity) if equity > 0 else 0.0)
        return PortfolioMTM(
            total_cash_usdc=cash,
            total_positions_mtm=total_pos,
            total_equity_usdc=equity,
            ts=self._clock.now_ms(),
        )

    def get_market_exposure_usdc(self, market_id: str) -> float:
        total = 0.0
        for plat in Platform:
            pos = self._positions.get((market_id, plat))
            if pos:
                prices = self._price_source(market_id, plat)
                if prices:
                    total += pos.mtm(*prices)
        return total

    def record_price_timestamp(self, market_id: str, platform: Platform, price_ts: int) -> None:
        pos = self._positions.get((market_id, platform))
        if pos is not None:
            pos.last_price_ts = price_ts

    def get_price_age_ms(self) -> int:
        latest = 0
        for pos in self._positions.values():
            if pos.last_price_ts > 0:
                latest = max(latest, pos.last_price_ts)
        if latest <= 0:
            return 0
        return max(0, self._clock.now_ms() - latest)

    def get_all_deltas(self) -> Dict[str, float]:
        markets = {mid for (mid, _) in self._positions}
        return {m: self.get_delta(m).net_delta for m in markets}

    def get_all_positions(self) -> List[DeltaResult]:
        """Return all positions as a list of DeltaResult-like objects for API consumption."""
        return [
            DeltaResult(
                market_id=mid,
                net_delta=pos.net_delta,
                yes_holdings_pm=pos.yes_qty if plat == Platform.POLYMARKET else 0.0,
                no_holdings_pm=pos.no_qty if plat == Platform.POLYMARKET else 0.0,
                yes_holdings_op=pos.yes_qty if plat == Platform.OPINION else 0.0,
                no_holdings_op=pos.no_qty if plat == Platform.OPINION else 0.0,
                avg_cost_yes_pm=pos.avg_cost_yes if plat == Platform.POLYMARKET else None,
                avg_cost_no_pm=pos.avg_cost_no if plat == Platform.POLYMARKET else None,
                avg_cost_yes_op=pos.avg_cost_yes if plat == Platform.OPINION else None,
                avg_cost_no_op=pos.avg_cost_no if plat == Platform.OPINION else None,
            )
            for (mid, plat), pos in self._positions.items()
        ]

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def available_capital(self) -> float:
        return max(0.0, self._cash_usdc - self._reserved_usdc)

    @property
    def reserved_capital(self) -> float:
        return self._reserved_usdc

    @property
    def cash_usdc(self) -> float:
        return self._cash_usdc

    @property
    def initial_capital(self) -> float:
        return self._initial_capital

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def build_snapshot(self) -> Dict[str, Any]:
        with self._sync_lock:
            cash = self._cash_usdc
            peak = self._peak_equity
            closed_pnl = self._closed_pnl
            snap_items = list(self._positions.items())
        entries = []
        total_pos = 0.0
        for (mid, plat), pos in snap_items:
            prices = self._price_source(mid, plat)
            if not prices:
                continue
            mtm_val = pos.mtm(*prices)
            total_pos += mtm_val
            entries.append(
                {
                    "market_id": mid,
                    "platform": plat.value,
                    "yes_qty": pos.yes_qty,
                    "no_qty": pos.no_qty,
                    "mtm_usdc": mtm_val,
                    "net_delta": pos.net_delta,
                    "realised_pnl": pos.realised_pnl,
                }
            )

        total_mtm = cash + total_pos
        drawdown = max(0.0, (peak - total_mtm) / peak) if peak > 0 else 0.0
        total_real = sum(p.realised_pnl for _, p in snap_items) + closed_pnl
        PORTFOLIO_MTM_USDC.set(total_mtm)
        PORTFOLIO_REALISED_PNL_USDC.set(total_real)
        return {
            "snapshot_id": str(uuid.uuid4()),
            "ts": self._clock.now_ms(),
            "positions": entries,
            "total_cash_usdc": self._cash_usdc,
            "total_mtm_usdc": total_mtm,
            "peak_equity_usdc": peak,
            "mtm_drawdown_pct": drawdown,
            "total_realised_pnl": total_real,
        }

    # ── Background ────────────────────────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(SNAPSHOT_INTERVAL_S)
            except asyncio.CancelledError:
                return
            await self._publish_snapshot()

    async def _publish_snapshot(self) -> None:
        if self._stream_writer is None:
            return
        try:
            snap = self.build_snapshot()
            await asyncio.wait_for(
                self._stream_writer("position_snapshots", snap),
                timeout=0.050,
            )
        except Exception as exc:
            logger.debug("Snapshot publish failed: %s", exc)

    def _equity_locked(self) -> float:
        """Called while holding _lock."""
        total_pos = 0.0
        for (mid, plat), pos in self._positions.items():
            prices = self._price_source(mid, plat)
            if prices:
                total_pos += pos.mtm(*prices)
        return self._cash_usdc + total_pos
