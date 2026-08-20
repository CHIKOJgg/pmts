"""portfolio/storage_postgres.py — PostgreSQL persistence backend."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple

from portfolio.manager import FillRecord
from src.enums import Platform, StrategyId

logger = logging.getLogger(__name__)

try:
    import asyncpg
except ImportError:
    asyncpg = None


def _run_async(coro: Any) -> Any:
    """Run a coroutine synchronously using a dedicated event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class PostgresPortfolioStore:
    def __init__(self, dsn: str) -> None:
        if asyncpg is None:
            raise ImportError("asyncpg is required for PostgreSQL support. Install with: pip install asyncpg")
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        await self._init_db()

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    def _require_pool(self) -> Any:
        pool = self._pool
        if pool is None:
            raise RuntimeError("Postgres pool not connected. Call connect() first.")
        return pool

    async def _init_db(self) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    market_id TEXT,
                    platform TEXT,
                    yes_qty REAL,
                    no_qty REAL,
                    avg_cost_yes REAL,
                    avg_cost_no REAL,
                    realised_pnl REAL,
                    PRIMARY KEY (market_id, platform)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    proposal_id TEXT,
                    order_id TEXT,
                    market_id TEXT,
                    platform TEXT,
                    side TEXT,
                    filled_usdc REAL,
                    fill_price REAL,
                    ts BIGINT,
                    realised_pnl REAL DEFAULT 0.0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value REAL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reservations (
                    proposal_id TEXT PRIMARY KEY,
                    amount REAL,
                    platform TEXT,
                    strategy_id TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_orders (
                    proposal_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    submission_json TEXT
                )
            """)

    # ── Sync public API (backed by _run_async helper) ──────────────────────

    def save_fill_and_position(
        self, fill: FillRecord, position: Any, cash_usdc: float, peak_equity: float, closed_pnl: float
    ) -> None:
        _run_async(self._save_fill_and_position_async(fill, position, cash_usdc, peak_equity, closed_pnl))

    async def _save_fill_and_position_async(
        self, fill: FillRecord, position: Any, cash_usdc: float, peak_equity: float, closed_pnl: float
    ) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO positions (market_id, platform, yes_qty, no_qty, avg_cost_yes, avg_cost_no, realised_pnl)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (market_id, platform) DO UPDATE SET
                    yes_qty = EXCLUDED.yes_qty,
                    no_qty = EXCLUDED.no_qty,
                    avg_cost_yes = EXCLUDED.avg_cost_yes,
                    avg_cost_no = EXCLUDED.avg_cost_no,
                    realised_pnl = EXCLUDED.realised_pnl
            """, position.market_id, position.platform.value, position.yes_qty, position.no_qty,
                position.avg_cost_yes, position.avg_cost_no, position.realised_pnl)

            fill_id = self._fill_id(fill)
            await conn.execute("""
                INSERT INTO fills (fill_id, proposal_id, order_id, market_id, platform, side, filled_usdc, fill_price, ts, realised_pnl)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (fill_id) DO UPDATE SET
                    order_id = EXCLUDED.order_id,
                    filled_usdc = EXCLUDED.filled_usdc,
                    fill_price = EXCLUDED.fill_price,
                    ts = EXCLUDED.ts,
                    realised_pnl = EXCLUDED.realised_pnl
            """, fill_id, fill.proposal_id, fill.order_id, fill.market_id, fill.platform.value,
                fill.side, fill.filled_usdc, fill.fill_price, fill.ts, fill.realised_pnl)

            await conn.execute("""
                INSERT INTO state (key, value) VALUES ($1, $2), ($3, $4), ($5, $6)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, "cash_usdc", cash_usdc, "peak_equity", peak_equity, "closed_pnl", closed_pnl)

    def load_state(self) -> Dict[str, Any]:
        async def _load() -> Dict[str, Any]:
            async with self._require_pool().acquire() as conn:
                rows = await conn.fetch("SELECT key, value FROM state")
                return {row["key"]: row["value"] for row in rows}
        return _run_async(_load())  # type: ignore[no-any-return]  # type: ignore[no-any-return]

    def save_order(self, proposal_id: str, submission_json: str, exchange_order_id: Optional[str]) -> None:
        async def _save() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute("""
                    INSERT INTO active_orders (proposal_id, exchange_order_id, submission_json)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (proposal_id) DO UPDATE SET
                        exchange_order_id = EXCLUDED.exchange_order_id,
                        submission_json = EXCLUDED.submission_json
                """, proposal_id, exchange_order_id, submission_json)
        _run_async(_save())

    def load_active_orders(self) -> List[Tuple[str, Optional[str], str]]:
        async def _load() -> List[Tuple[str, Optional[str], str]]:
            async with self._require_pool().acquire() as conn:
                rows = await conn.fetch("SELECT proposal_id, exchange_order_id, submission_json FROM active_orders")
                return [(r["proposal_id"], r["exchange_order_id"], r["submission_json"]) for r in rows]
        return _run_async(_load())  # type: ignore[no-any-return]

    def update_order_exchange_id(self, proposal_id: str, exchange_order_id: str) -> None:
        async def _update() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute(
                    "UPDATE active_orders SET exchange_order_id = $1 WHERE proposal_id = $2",
                    exchange_order_id,
                    proposal_id,
                )
        _run_async(_update())

    def remove_order(self, proposal_id: str) -> None:
        async def _remove() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute("DELETE FROM active_orders WHERE proposal_id = $1", proposal_id)
        _run_async(_remove())

    def save_reservation(self, proposal_id: str, amount: float, platform: Platform, strategy_id: StrategyId) -> None:
        async def _save() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute("""
                    INSERT INTO reservations (proposal_id, amount, platform, strategy_id)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (proposal_id) DO UPDATE SET
                        amount = EXCLUDED.amount,
                        platform = EXCLUDED.platform,
                        strategy_id = EXCLUDED.strategy_id
                """, proposal_id, amount, platform.value, strategy_id.value)
        _run_async(_save())

    def remove_reservation(self, proposal_id: str) -> None:
        async def _remove() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute("DELETE FROM reservations WHERE proposal_id = $1", proposal_id)
        _run_async(_remove())

    def load_reservations(self) -> Dict[str, Tuple[float, Platform, StrategyId]]:
        async def _load() -> Dict[str, Tuple[float, Platform, StrategyId]]:
            async with self._require_pool().acquire() as conn:
                rows = await conn.fetch("SELECT proposal_id, amount, platform, strategy_id FROM reservations")
                return {
                    r["proposal_id"]: (r["amount"], Platform(r["platform"]), StrategyId(r["strategy_id"]))
                    for r in rows
                }
        return _run_async(_load())  # type: ignore[no-any-return]

    def save_kill_switch(self, active: bool) -> None:
        async def _save() -> None:
            async with self._require_pool().acquire() as conn:
                await conn.execute("""
                    INSERT INTO state (key, value) VALUES ($1, $2)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, "kill_switch_active", 1.0 if active else 0.0)
        _run_async(_save())

    def load_kill_switch(self) -> bool:
        async def _load() -> bool:
            async with self._require_pool().acquire() as conn:
                row = await conn.fetchrow("SELECT value FROM state WHERE key = $1", "kill_switch_active")
                return bool(row["value"]) if row else False
        return _run_async(_load())  # type: ignore[no-any-return]

    def _fill_id(self, fill: FillRecord) -> str:
        raw = f"{fill.proposal_id}|{fill.order_id}|{fill.ts}|{fill.filled_usdc:.8f}|{fill.fill_price:.8f}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_healthy(self) -> bool:
        try:
            if self._pool is None:
                return False
            return True
        except Exception:
            return False
