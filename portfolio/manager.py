"""portfolio/manager.py — Position tracking, cost basis, MTM, and P&L."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from src.errors import NegativeHoldings
from src.types import Outcome, Platform, Side

logger = logging.getLogger(__name__)

DUST_FLOOR:          float = 1e-9
SNAPSHOT_INTERVAL_S: float = 60.0

_PriceSource = Callable[[str, Platform], Optional[Tuple[float, float]]]


# ─────────────────────────────────────────────────────────────────────────────
# Public DTOs returned by PortfolioManager
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FillRecord:
    proposal_id: str
    order_id:    str
    market_id:   str
    platform:    Platform
    side:        str    # Side.value
    filled_usdc: float
    fill_price:  float
    ts:          int


@dataclass(frozen=True)
class RedemptionRecord:
    market_id:     str
    platform:      Platform
    outcome:       Outcome
    usdc_received: float
    ts:            int


@dataclass(frozen=True)
class DeltaResult:
    market_id:       str
    net_delta:       float
    yes_holdings_pm: float
    no_holdings_pm:  float
    yes_holdings_op: float
    no_holdings_op:  float


@dataclass(frozen=True)
class PortfolioMTM:
    total_cash_usdc:     float
    total_positions_mtm: float
    total_equity_usdc:   float
    ts:                  int


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
        "market_id", "platform",
        "yes_qty", "no_qty",
        "avg_cost_yes", "avg_cost_no",
        "realised_pnl",
    )

    def __init__(self, market_id: str, platform: Platform) -> None:
        self.market_id:    str            = market_id
        self.platform:     Platform       = platform
        self.yes_qty:      float          = 0.0
        self.no_qty:       float          = 0.0
        self.avg_cost_yes: Optional[float] = None
        self.avg_cost_no:  Optional[float] = None
        self.realised_pnl: float          = 0.0

    @property
    def net_delta(self) -> float:
        return self.yes_qty - self.no_qty

    @property
    def is_flat(self) -> bool:
        return self.yes_qty < DUST_FLOOR and self.no_qty < DUST_FLOOR

    def apply_fill(self, side: Side, tokens: float, price: float) -> None:
        if side == Side.BUY_YES:
            self.yes_qty, self.avg_cost_yes = _wavg(
                self.yes_qty, self.avg_cost_yes, tokens, price
            )
        elif side == Side.BUY_NO:
            self.no_qty, self.avg_cost_no = _wavg(
                self.no_qty, self.avg_cost_no, tokens, price
            )
        elif side == Side.SELL_YES:
            if tokens > self.yes_qty + DUST_FLOOR:
                raise NegativeHoldings(
                    f"SELL_YES {tokens:.6f} > holdings {self.yes_qty:.6f}",
                    market_id=self.market_id, platform=self.platform.value,
                    token_side="yes", current_holdings=self.yes_qty, fill_size=tokens,
                )
            if self.avg_cost_yes is not None:
                self.realised_pnl += tokens * (price - self.avg_cost_yes)
            self.yes_qty = max(0.0, self.yes_qty - tokens)
            if self.yes_qty < DUST_FLOOR:
                self.yes_qty      = 0.0
                self.avg_cost_yes = None
        else:  # SELL_NO
            if tokens > self.no_qty + DUST_FLOOR:
                raise NegativeHoldings(
                    f"SELL_NO {tokens:.6f} > holdings {self.no_qty:.6f}",
                    market_id=self.market_id, platform=self.platform.value,
                    token_side="no", current_holdings=self.no_qty, fill_size=tokens,
                )
            if self.avg_cost_no is not None:
                self.realised_pnl += tokens * (price - self.avg_cost_no)
            self.no_qty = max(0.0, self.no_qty - tokens)
            if self.no_qty < DUST_FLOOR:
                self.no_qty      = 0.0
                self.avg_cost_no = None

    def mtm(self, yes_mid: float, no_mid: float) -> float:
        return self.yes_qty * yes_mid + self.no_qty * no_mid

    def close_on_redemption(self, outcome: str, usdc_received: float) -> None:
        qty  = self.yes_qty if outcome == "yes" else self.no_qty
        cost = (self.avg_cost_yes if outcome == "yes" else self.avg_cost_no) or 0.0
        self.realised_pnl += usdc_received - cost * qty
        self.yes_qty       = 0.0
        self.no_qty        = 0.0
        self.avg_cost_yes  = None
        self.avg_cost_no   = None


def _wavg(
    prev_qty: float, prev_avg: Optional[float],
    fill_qty: float, fill_price: float,
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
        price_source:      _PriceSource,
        stream_writer:     Optional[Callable] = None,
        store=None,
    ) -> None:
        if initial_cash_usdc < 0:
            raise ValueError(f"initial_cash_usdc must be ≥ 0, got {initial_cash_usdc}")

        self._lock:          asyncio.Lock                            = asyncio.Lock()
        self._positions:     Dict[Tuple[str, Platform], _Position]  = {}
        self._cash_usdc:     float = initial_cash_usdc
        self._reserved_usdc: float = 0.0
        self._peak_equity:   float = initial_cash_usdc
        self._closed_pnl:    float = 0.0
        self._price_source:  _PriceSource     = price_source
        self._stream_writer: Optional[Callable] = stream_writer
        self._store = store

        if self._store:
            state = self._store.load_state()
            if state["cash_usdc"] is not None:
                self._cash_usdc = state["cash_usdc"]
            if state["peak_equity"] is not None:
                self._peak_equity = state["peak_equity"]
            self._closed_pnl = state.get("closed_pnl", 0.0)
            self._positions = state.get("positions", {})
            logger.info("Loaded portfolio state from SQLite. Cash: $%.2f, Positions: %d", 
                        self._cash_usdc, len(self._positions))

        self._tasks:   list[asyncio.Task] = []
        self._stopped: bool               = False

        self.fill_count:       int = 0
        self.redemption_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopped = False
        self._tasks.append(asyncio.create_task(
            self._snapshot_loop(), name="portfolio-snapshot"
        ))

    async def stop(self) -> None:
        self._stopped = True
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ── Mutations (locked) ────────────────────────────────────────────────────

    async def record_fill(self, fill: FillRecord) -> None:
        if fill.fill_price <= 0:
            logger.warning("record_fill: fill_price is %s — skipping", fill.fill_price)
            return
        tokens = fill.filled_usdc / fill.fill_price
        side   = Side(fill.side)
        key    = (fill.market_id, fill.platform)

        async with self._lock:
            if key not in self._positions:
                self._positions[key] = _Position(fill.market_id, fill.platform)
            self._positions[key].apply_fill(side, tokens, fill.fill_price)

            if side.is_buy:
                self._cash_usdc = max(0.0, self._cash_usdc - fill.filled_usdc)
            else:
                self._cash_usdc += fill.filled_usdc

            equity = self._equity_locked()
            if equity > self._peak_equity:
                self._peak_equity = equity

            self.fill_count += 1

            if self._store:
                self._store.save_fill_and_position(
                    fill, self._positions[key], self._cash_usdc, self._peak_equity, self._closed_pnl
                )

        asyncio.create_task(
            self._publish_snapshot(),
            name=f"snap-{fill.proposal_id[:8]}",
        )

    async def record_redemption(self, redemption: RedemptionRecord) -> None:
        key = (redemption.market_id, redemption.platform)
        async with self._lock:
            pos = self._positions.get(key)
            if pos is None:
                logger.warning("record_redemption: no position for %s/%s",
                               redemption.market_id, redemption.platform.value)
                return
            pos.close_on_redemption(redemption.outcome.value, redemption.usdc_received)
            self._cash_usdc += redemption.usdc_received
            self._closed_pnl += pos.realised_pnl
            if pos.is_flat:
                del self._positions[key]
            equity = self._equity_locked()
            if equity > self._peak_equity:
                self._peak_equity = equity
            self.redemption_count += 1
            
            if self._store:
                pos_to_save = None if pos.is_flat else pos
                self._store.save_redemption(
                    redemption.market_id, redemption.platform,
                    self._cash_usdc, self._peak_equity, self._closed_pnl, pos_to_save
                )

    # ── Capital reservation (called synchronously by RiskEngine) ─────────────

    async def reserve_capital(self, amount: float) -> None:
        async with self._lock:
            self._reserved_usdc += amount

    async def release_capital(self, amount: float) -> None:
        async with self._lock:
            self._reserved_usdc = max(0.0, self._reserved_usdc - amount)

    # ── Hot-path reads (lock-free, synchronous) ───────────────────────────────

    def get_delta(self, market_id: str) -> DeltaResult:
        pm = self._positions.get((market_id, Platform.POLYMARKET))
        op = self._positions.get((market_id, Platform.OPINION))
        y_pm = pm.yes_qty if pm else 0.0
        n_pm = pm.no_qty  if pm else 0.0
        y_op = op.yes_qty if op else 0.0
        n_op = op.no_qty  if op else 0.0
        return DeltaResult(
            market_id=market_id,
            net_delta=(y_pm + y_op) - (n_pm + n_op),
            yes_holdings_pm=y_pm, no_holdings_pm=n_pm,
            yes_holdings_op=y_op, no_holdings_op=n_op,
        )

    def get_portfolio_mtm(self) -> PortfolioMTM:
        total_pos = 0.0
        for (mid, plat), pos in self._positions.items():
            prices = self._price_source(mid, plat)
            if prices:
                total_pos += pos.mtm(*prices)
        equity = self._cash_usdc + total_pos
        return PortfolioMTM(
            total_cash_usdc=self._cash_usdc,
            total_positions_mtm=total_pos,
            total_equity_usdc=equity,
            ts=_now_ms(),
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

    def get_all_deltas(self) -> Dict[str, float]:
        markets = {mid for (mid, _) in self._positions}
        return {m: self.get_delta(m).net_delta for m in markets}

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
    def peak_equity(self) -> float:
        return self._peak_equity

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def build_snapshot(self) -> dict:
        entries   = []
        total_pos = 0.0
        for (mid, plat), pos in self._positions.items():
            prices = self._price_source(mid, plat)
            if not prices:
                continue
            mtm_val = pos.mtm(*prices)
            total_pos += mtm_val
            entries.append({
                "market_id": mid, "platform": plat.value,
                "yes_qty": pos.yes_qty, "no_qty": pos.no_qty,
                "mtm_usdc": mtm_val, "net_delta": pos.net_delta,
                "realised_pnl": pos.realised_pnl,
            })

        total_mtm   = self._cash_usdc + total_pos
        peak        = self._peak_equity
        drawdown    = max(0.0, (peak - total_mtm) / peak) if peak > 0 else 0.0
        total_real  = (
            sum(p.realised_pnl for p in self._positions.values()) + self._closed_pnl
        )
        return {
            "snapshot_id": str(uuid.uuid4()),
            "ts": _now_ms(),
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


def _now_ms() -> int:
    return int(time.time() * 1000)