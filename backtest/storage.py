"""backtest/storage.py — Persist and compare backtest results."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_BACKTEST_DB = "backtest_results.db"


@dataclass
class BacktestRunRecord:
    run_id: str
    timestamp: float
    config_json: str
    result_json: str
    total_return_pct: float
    sharpe: Optional[float]
    max_drawdown_pct: float
    total_pnl: float
    total_ticks: int
    markets: str


class BacktestResultStore:
    """Persists backtest results to SQLite for comparison across runs."""

    def __init__(self, db_path: str = _BACKTEST_DB) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY,
                timestamp REAL,
                config_json TEXT,
                result_json TEXT,
                total_return_pct REAL,
                sharpe REAL,
                max_drawdown_pct REAL,
                total_pnl REAL,
                total_ticks INTEGER,
                markets TEXT
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_backtest_timestamp
            ON backtest_runs(timestamp DESC)
        """)
        self._conn.commit()

    def save_run(
        self,
        run_id: str,
        config: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        total_return = result.get("total_return", 0.0)
        total_ticks = result.get("total_ticks", 0)
        self._conn.execute("""
            INSERT OR REPLACE INTO backtest_runs
            (run_id, timestamp, config_json, result_json, total_return_pct, sharpe, max_drawdown_pct, total_pnl, total_ticks, markets)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            time.time(),
            json.dumps(config, default=str),
            json.dumps(result, default=str),
            total_return * 100.0,
            result.get("sharpe_ratio"),
            result.get("max_drawdown", 0.0) * 100.0,
            result.get("total_pnl", 0.0),
            total_ticks,
            ",".join(result.get("market_ids", [])),
        ))
        self._conn.commit()

    def get_recent_runs(self, limit: int = 20) -> List[BacktestRunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM backtest_runs ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            BacktestRunRecord(
                run_id=row["run_id"],
                timestamp=row["timestamp"],
                config_json=row["config_json"],
                result_json=row["result_json"],
                total_return_pct=row["total_return_pct"],
                sharpe=row["sharpe"],
                max_drawdown_pct=row["max_drawdown_pct"],
                total_pnl=row["total_pnl"],
                total_ticks=row["total_ticks"],
                markets=row["markets"],
            )
            for row in rows
        ]

    def get_best_runs(self, metric: str = "sharpe", limit: int = 10) -> List[BacktestRunRecord]:
        valid_metrics = {"sharpe", "total_return_pct", "max_drawdown_pct"}
        order_col = metric if metric in valid_metrics else "sharpe"
        order_dir = "DESC" if metric != "max_drawdown_pct" else "ASC"
        rows = self._conn.execute(
            f"SELECT * FROM backtest_runs ORDER BY {order_col} {order_dir} NULLS LAST LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            BacktestRunRecord(
                run_id=row["run_id"],
                timestamp=row["timestamp"],
                config_json=row["config_json"],
                result_json=row["result_json"],
                total_return_pct=row["total_return_pct"],
                sharpe=row["sharpe"],
                max_drawdown_pct=row["max_drawdown_pct"],
                total_pnl=row["total_pnl"],
                total_ticks=row["total_ticks"],
                markets=row["markets"],
            )
            for row in rows
        ]

    def compare_runs(self, run_ids: List[str]) -> str:
        records = []
        for rid in run_ids:
            row = self._conn.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?", (rid,)
            ).fetchone()
            if row:
                records.append(BacktestRunRecord(**dict(row)))
        if not records:
            return "No records found"
        lines = ["═══ BACKTEST COMPARISON ═══", ""]
        header = f"{'Run ID':<20} {'Return':>8} {'Sharpe':>8} {'Max DD':>8} {'PnL':>10} {'Ticks':>6}"
        lines.append(header)
        lines.append("-" * len(header))
        for r in records:
            sr = f"{r.sharpe:.2f}" if r.sharpe is not None else "N/A"
            lines.append(
                f"{r.run_id:<20} {r.total_return_pct:>7.2f}% {sr:>8} {r.max_drawdown_pct:>7.2f}% "
                f"${r.total_pnl:>+8.2f} {r.total_ticks:>6}"
            )
        return "\n".join(lines)

    def close(self) -> None:
        self._conn.close()
