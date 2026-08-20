"""portfolio/storage_postgres.py — PostgreSQL persistence backend."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from portfolio.manager import FillRecord, _Position
from src.enums import Platform, StrategyId

logger = logging.getLogger(__name__)


try:
    import asyncpg
except ImportError:
    asyncpg = None


class PostgresPortfolioStore:
    def __init__(self, dsn: str) -> None:
        if asyncpg is None:
            raise ImportError("asyncpg is required for PostgreSQL support. Install with: pip install asyncpg")
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

    # ── Background event loop (persistent, dedicated thread) ──────────────────

    def _ensure_loop(self) -> None:
        """Start a daemon thread with its own persistent event loop (once)."""
        if self._loop is not None and self._loop.is_running():
            return
        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(target=_run, name="postgres-store", daemon=True)
        self._loop_thread.start()
        ready.wait()

    def _run(self, coro: Any) -> Any:
        """Schedule a coroutine on the dedicated background loop and wait for it."""
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("Postgres background loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result()

    async def connect(self) -> None:
        """Create the asyncpg pool on the background loop (async-safe)."""
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
        await asyncio.wrap_future(fut)

    async def _connect_async(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        await self._init_db()

    async def close(self) -> None:
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
            await asyncio.wrap_future(fut)
        except Exception as exc:
            logger.error("Failed to close Postgres pool: %s", exc)
        finally:
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            self._loop = None

    async def _close_async(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

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

    # ── Sync public API (backed by the dedicated background loop) ─────────────

    def save_fill_and_position(
        self, fill: FillRecord, position: Any, cash_usdc: float, peak_equity: float, closed_pnl: float
    ) -> None:
        try:
            self._run(self._save_fill_and_position_async(fill, position, cash_usdc, peak_equity, closed_pnl))
        except Exception as exc:
            logger.error("Failed to save fill/position to Postgres: %s", exc)

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
        try:
            return self._run(self._load_state_async())
        except Exception as exc:
            logger.error("Failed to load state from Postgres: %s", exc)
            return {"cash_usdc": None, "peak_equity": None, "closed_pnl": 0.0, "positions": {}}

    async def _load_state_async(self) -> Dict[str, Any]:
        async with self._require_pool().acquire() as conn:
            state: Dict[str, Any] = {"cash_usdc": None, "peak_equity": None, "closed_pnl": 0.0}
            rows = await conn.fetch("SELECT key, value FROM state")
            for row in rows:
                state[row["key"]] = row["value"]

            positions: Dict[Tuple[str, Platform], Any] = {}
            pos_rows = await conn.fetch(
                "SELECT market_id, platform, yes_qty, no_qty, avg_cost_yes, avg_cost_no, realised_pnl FROM positions"
            )
            for row in pos_rows:
                plat = Platform(row["platform"])
                pos = _Position(row["market_id"], plat)
                pos.yes_qty = row["yes_qty"]
                pos.no_qty = row["no_qty"]
                pos.avg_cost_yes = row["avg_cost_yes"]
                pos.avg_cost_no = row["avg_cost_no"]
                pos.realised_pnl = row["realised_pnl"]
                positions[(row["market_id"], plat)] = pos

            state["positions"] = positions
            return state

    def save_order(self, proposal_id: str, submission_json: str, exchange_order_id: Optional[str]) -> None:
        try:
            self._run(self._save_order_async(proposal_id, submission_json, exchange_order_id))
        except Exception as exc:
            logger.error("Failed to save order to Postgres: %s", exc)

    async def _save_order_async(self, proposal_id: str, submission_json: str, exchange_order_id: Optional[str]) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO active_orders (proposal_id, exchange_order_id, submission_json)
                VALUES ($1, $2, $3)
                ON CONFLICT (proposal_id) DO UPDATE SET
                    exchange_order_id = EXCLUDED.exchange_order_id,
                    submission_json = EXCLUDED.submission_json
            """, proposal_id, exchange_order_id, submission_json)

    def load_active_orders(self) -> List[Tuple[str, Optional[str], str]]:
        try:
            return self._run(self._load_active_orders_async())
        except Exception as exc:
            logger.error("Failed to load active orders from Postgres: %s", exc)
            return []

    async def _load_active_orders_async(self) -> List[Tuple[str, Optional[str], str]]:
        async with self._require_pool().acquire() as conn:
            rows = await conn.fetch("SELECT proposal_id, exchange_order_id, submission_json FROM active_orders")
            return [(r["proposal_id"], r["exchange_order_id"], r["submission_json"]) for r in rows]

    def update_order_exchange_id(self, proposal_id: str, exchange_order_id: str) -> None:
        try:
            self._run(self._update_order_exchange_id_async(proposal_id, exchange_order_id))
        except Exception as exc:
            logger.error("Failed to update order exchange ID in Postgres: %s", exc)

    async def _update_order_exchange_id_async(self, proposal_id: str, exchange_order_id: str) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute(
                "UPDATE active_orders SET exchange_order_id = $1 WHERE proposal_id = $2",
                exchange_order_id,
                proposal_id,
            )

    def remove_order(self, proposal_id: str) -> None:
        try:
            self._run(self._remove_order_async(proposal_id))
        except Exception as exc:
            logger.error("Failed to remove order from Postgres: %s", exc)

    async def _remove_order_async(self, proposal_id: str) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute("DELETE FROM active_orders WHERE proposal_id = $1", proposal_id)

    def save_reservation(self, proposal_id: str, amount: float, platform: Platform, strategy_id: StrategyId) -> None:
        try:
            self._run(self._save_reservation_async(proposal_id, amount, platform, strategy_id))
        except Exception as exc:
            logger.error("Failed to save reservation to Postgres: %s", exc)

    async def _save_reservation_async(
        self, proposal_id: str, amount: float, platform: Platform, strategy_id: StrategyId
    ) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO reservations (proposal_id, amount, platform, strategy_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (proposal_id) DO UPDATE SET
                    amount = EXCLUDED.amount,
                    platform = EXCLUDED.platform,
                    strategy_id = EXCLUDED.strategy_id
            """, proposal_id, amount, platform.value, strategy_id.value)

    def remove_reservation(self, proposal_id: str) -> None:
        try:
            self._run(self._remove_reservation_async(proposal_id))
        except Exception as exc:
            logger.error("Failed to remove reservation from Postgres: %s", exc)

    async def _remove_reservation_async(self, proposal_id: str) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute("DELETE FROM reservations WHERE proposal_id = $1", proposal_id)

    def load_reservations(self) -> Dict[str, Tuple[float, Platform, StrategyId]]:
        try:
            return self._run(self._load_reservations_async())
        except Exception as exc:
            logger.error("Failed to load reservations from Postgres: %s", exc)
            return {}

    async def _load_reservations_async(self) -> Dict[str, Tuple[float, Platform, StrategyId]]:
        async with self._require_pool().acquire() as conn:
            rows = await conn.fetch("SELECT proposal_id, amount, platform, strategy_id FROM reservations")
            return {
                r["proposal_id"]: (r["amount"], Platform(r["platform"]), StrategyId(r["strategy_id"]))
                for r in rows
            }

    def save_kill_switch(self, active: bool) -> None:
        try:
            self._run(self._save_kill_switch_async(active))
        except Exception as exc:
            logger.error("Failed to save kill switch state to Postgres: %s", exc)

    async def _save_kill_switch_async(self, active: bool) -> None:
        async with self._require_pool().acquire() as conn:
            await conn.execute("""
                INSERT INTO state (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, "kill_switch_active", 1.0 if active else 0.0)

    def load_kill_switch(self) -> bool:
        try:
            return self._run(self._load_kill_switch_async())
        except Exception as exc:
            logger.error("Failed to load kill switch state from Postgres: %s", exc)
            return False

    async def _load_kill_switch_async(self) -> bool:
        async with self._require_pool().acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM state WHERE key = $1", "kill_switch_active")
            return bool(row["value"]) if row else False

    def _fill_id(self, fill: FillRecord) -> str:
        raw = f"{fill.proposal_id}|{fill.order_id}|{fill.ts}|{fill.filled_usdc:.8f}|{fill.fill_price:.8f}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_healthy(self) -> bool:
        if self._pool is None or self._loop is None or not self._loop.is_running():
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(self._ping_async(), self._loop)
            return fut.result(timeout=5)
        except Exception as exc:
            logger.error("Postgres health check failed: %s", exc)
            return False

    async def _ping_async(self) -> bool:
        async with self._require_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
