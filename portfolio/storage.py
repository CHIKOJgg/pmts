import sqlite3
import logging
import hashlib
from typing import Dict, Tuple, Optional, List


from portfolio.manager import _Position, FillRecord
from src.types import Platform, StrategyId

logger = logging.getLogger(__name__)

class SqlitePortfolioStore:
    """
    SQLite persistence for PortfolioManager (Step 5).
    Uses WAL mode for high concurrency and fast writes.
    Supports context manager protocol for safe resource cleanup.
    """

    def __init__(self, db_path: str = "portfolio.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        self._init_db()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self) -> None:
        """Close the SQLite connection safely."""
        if not self._closed and self._conn:
            try:
                self._conn.close()
            except Exception as exc:
                logger.error("Failed to close SQLite connection: %s", exc)
            finally:
                self._closed = True

    def is_healthy(self) -> bool:
        """Check if SQLite is reachable."""
        if self._closed:
            return False
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._conn:
            self._conn.execute('''
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
            ''')
            self._conn.execute('''
                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    proposal_id TEXT,
                    order_id TEXT,
                    market_id TEXT,
                    platform TEXT,
                    side TEXT,
                    filled_usdc REAL,
                    fill_price REAL,
                    ts INTEGER
                )
            ''')
            self._migrate_fills_schema()
            self._conn.execute('''
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value REAL
                )
            ''')
            self._conn.execute('''
                CREATE TABLE IF NOT EXISTS reservations (
                    proposal_id TEXT PRIMARY KEY,
                    amount REAL,
                    platform TEXT,
                    strategy_id TEXT
                )
            ''')
            self._conn.execute('''
                CREATE TABLE IF NOT EXISTS active_orders (
                    proposal_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    submission_json TEXT
                )
            ''')

    def save_fill_and_position(
        self,
        fill: FillRecord,
        position: _Position,
        cash_usdc: float,
        peak_equity: float,
        closed_pnl: float
    ) -> None:
        """Atomic write of a fill and resulting position/state updates."""
        try:
            fill_id = self._fill_id(fill)
            with self._conn:
                self._conn.execute('''
                    INSERT OR IGNORE INTO fills
                    (fill_id, proposal_id, order_id, market_id, platform, side, filled_usdc, fill_price, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    fill_id,
                    fill.proposal_id, fill.order_id, fill.market_id, fill.platform.value,
                    fill.side, fill.filled_usdc, fill.fill_price, fill.ts
                ))
                
                self._conn.execute('''
                    INSERT OR REPLACE INTO positions
                    (market_id, platform, yes_qty, no_qty, avg_cost_yes, avg_cost_no, realised_pnl)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    position.market_id, position.platform.value, position.yes_qty, position.no_qty,
                    position.avg_cost_yes, position.avg_cost_no, position.realised_pnl
                ))

                self._conn.executemany('''
                    INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)
                ''', [
                    ("cash_usdc", cash_usdc),
                    ("peak_equity", peak_equity),
                    ("closed_pnl", closed_pnl)
                ])
        except Exception as exc:
            logger.error("Failed to save fill/position to SQLite: %s", exc)

    def save_redemption(
        self,
        market_id: str,
        platform: Platform,
        cash_usdc: float,
        peak_equity: float,
        closed_pnl: float,
        position: Optional[_Position] = None
    ) -> None:
        """Atomic write for position redemption."""
        try:
            with self._conn:
                if position and not position.is_flat:
                    self._conn.execute('''
                        INSERT OR REPLACE INTO positions
                        (market_id, platform, yes_qty, no_qty, avg_cost_yes, avg_cost_no, realised_pnl)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        position.market_id, position.platform.value, position.yes_qty, position.no_qty,
                        position.avg_cost_yes, position.avg_cost_no, position.realised_pnl
                    ))
                else:
                    self._conn.execute('''
                        DELETE FROM positions WHERE market_id = ? AND platform = ?
                    ''', (market_id, platform.value))

                self._conn.executemany('''
                    INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)
                ''', [
                    ("cash_usdc", cash_usdc),
                    ("peak_equity", peak_equity),
                    ("closed_pnl", closed_pnl)
                ])
        except Exception as exc:
            logger.error("Failed to save redemption to SQLite: %s", exc)

    def load_state(self) -> dict:
        """Load the entire portfolio state on startup."""
        cur = self._conn.cursor()
        
        state = {"cash_usdc": None, "peak_equity": None, "closed_pnl": 0.0}
        for row in cur.execute("SELECT key, value FROM state"):
            state[row["key"]] = row["value"]

        positions = {}
        for row in cur.execute("SELECT * FROM positions"):
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

    # ── Kill Switch Persistence ───────────────────────────────────────────────

    def save_kill_switch(self, active: bool) -> None:
        """Persist kill switch status to the state table."""
        try:
            with self._conn:
                self._conn.execute('''
                    INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)
                ''', ("kill_switch_active", 1.0 if active else 0.0))
        except Exception as exc:
            logger.error("Failed to save kill switch state to SQLite: %s", exc)

    def load_kill_switch(self) -> bool:
        """Load kill switch status from the state table."""
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT value FROM state WHERE key = ?", ("kill_switch_active",))
            row = cur.fetchone()
            return bool(row["value"]) if row else False
        except Exception as exc:
            logger.error("Failed to load kill switch state from SQLite: %s", exc)
            return False

    # ── Risk Reservations ─────────────────────────────────────────────────────

    def save_reservation(self, proposal_id: str, amount: float, platform: Platform, strategy_id: StrategyId) -> None:
        try:
            with self._conn:
                self._conn.execute('''
                    INSERT OR REPLACE INTO reservations (proposal_id, amount, platform, strategy_id)
                    VALUES (?, ?, ?, ?)
                ''', (proposal_id, amount, platform.value, strategy_id.value))
        except Exception as exc:
            logger.error("Failed to save reservation to SQLite: %s", exc)

    def remove_reservation(self, proposal_id: str) -> None:
        try:
            with self._conn:
                self._conn.execute('DELETE FROM reservations WHERE proposal_id = ?', (proposal_id,))
        except Exception as exc:
            logger.error("Failed to remove reservation from SQLite: %s", exc)

    def load_reservations(self) -> Dict[str, Tuple[float, Platform, StrategyId]]:
        cur = self._conn.cursor()
        reservations = {}
        for row in cur.execute("SELECT proposal_id, amount, platform, strategy_id FROM reservations"):
            reservations[row["proposal_id"]] = (
                row["amount"],
                Platform(row["platform"]),
                StrategyId(row["strategy_id"])
            )
        return reservations

    # ── Active Orders Persistence ─────────────────────────────────────────────

    def save_order(self, proposal_id: str, submission_json: str, exchange_order_id: Optional[str] = None) -> None:
        try:
            with self._conn:
                self._conn.execute('''
                    INSERT OR REPLACE INTO active_orders (proposal_id, exchange_order_id, submission_json)
                    VALUES (?, ?, ?)
                ''', (proposal_id, exchange_order_id, submission_json))
        except Exception as exc:
            logger.error("Failed to save order to SQLite: %s", exc)

    def update_order_exchange_id(self, proposal_id: str, exchange_order_id: str) -> None:
        try:
            with self._conn:
                self._conn.execute('''
                    UPDATE active_orders SET exchange_order_id = ? WHERE proposal_id = ?
                ''', (exchange_order_id, proposal_id))
        except Exception as exc:
            logger.error("Failed to update order exchange ID in SQLite: %s", exc)

    def remove_order(self, proposal_id: str) -> None:
        try:
            with self._conn:
                self._conn.execute('DELETE FROM active_orders WHERE proposal_id = ?', (proposal_id,))
        except Exception as exc:
            logger.error("Failed to remove order from SQLite: %s", exc)

    def load_active_orders(self) -> List[Tuple[str, Optional[str], str]]:
        """Returns List[(proposal_id, exchange_order_id, submission_json)]"""
        cur = self._conn.cursor()
        orders = []
        for row in cur.execute("SELECT proposal_id, exchange_order_id, submission_json FROM active_orders"):
            orders.append((row["proposal_id"], row["exchange_order_id"], row["submission_json"]))
        return orders

    def _migrate_fills_schema(self) -> None:
        """Upgrade old fills table where proposal_id was the primary key."""
        cur = self._conn.cursor()
        cols = [row["name"] for row in cur.execute("PRAGMA table_info(fills)")]
        if "fill_id" in cols:
            return

        with self._conn:
            self._conn.execute("ALTER TABLE fills RENAME TO fills_old")
            self._conn.execute('''
                CREATE TABLE fills (
                    fill_id TEXT PRIMARY KEY,
                    proposal_id TEXT,
                    order_id TEXT,
                    market_id TEXT,
                    platform TEXT,
                    side TEXT,
                    filled_usdc REAL,
                    fill_price REAL,
                    ts INTEGER
                )
            ''')
            for row in self._conn.execute("SELECT * FROM fills_old"):
                fill_id = self._fill_id_from_parts(
                    row["proposal_id"],
                    row["order_id"],
                    row["ts"],
                    row["filled_usdc"],
                    row["fill_price"],
                )
                self._conn.execute('''
                    INSERT OR IGNORE INTO fills
                    (fill_id, proposal_id, order_id, market_id, platform, side, filled_usdc, fill_price, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    fill_id,
                    row["proposal_id"],
                    row["order_id"],
                    row["market_id"],
                    row["platform"],
                    row["side"],
                    row["filled_usdc"],
                    row["fill_price"],
                    row["ts"],
                ))
            self._conn.execute("DROP TABLE fills_old")

    def _fill_id(self, fill: FillRecord) -> str:
        return self._fill_id_from_parts(
            fill.proposal_id,
            fill.order_id,
            fill.ts,
            fill.filled_usdc,
            fill.fill_price,
        )

    @staticmethod
    def _fill_id_from_parts(proposal_id: str, order_id: str, ts: int, filled_usdc: float, fill_price: float) -> str:
        raw = f"{proposal_id}|{order_id}|{ts}|{filled_usdc:.8f}|{fill_price:.8f}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
