"""compliance/trade_report.py — Trade and position reporting for compliance."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Any, List, Optional

from portfolio.manager import FillRecord


@dataclass
class TradeReportEntry:
    timestamp: int
    market_id: str
    platform: str
    side: str
    size_usdc: float
    fill_price: float
    order_id: str
    proposal_id: str


@dataclass
class PositionSnapshot:
    timestamp: int
    market_id: str
    platform: str
    yes_qty: float
    no_qty: float
    avg_cost_yes: float
    avg_cost_no: float
    unrealized_pnl: float


class TradeReporter:
    def __init__(self, report_dir: str = "reports") -> None:
        self._report_dir = report_dir
        os.makedirs(report_dir, exist_ok=True)

    def generate_trade_report(
        self,
        fills: List[FillRecord],
        output_format: str = "csv",
        filename: Optional[str] = None,
    ) -> str:
        entries = [
            TradeReportEntry(
                timestamp=f.ts,
                market_id=f.market_id,
                platform=f.platform,
                side=f.side,
                size_usdc=f.filled_usdc,
                fill_price=f.fill_price,
                order_id=f.order_id or "",
                proposal_id=f.proposal_id,
            )
            for f in fills
        ]

        if output_format == "csv":
            return self._write_csv(entries, filename or "trade_report.csv")
        elif output_format == "json":
            return self._write_json(entries, filename or "trade_report.json")
        else:
            raise ValueError(f"Unsupported format: {output_format}")

    def generate_position_report(
        self,
        positions: List[Any],
        output_format: str = "csv",
        filename: Optional[str] = None,
        mark_prices: Optional[Dict[str, Any]] = None,
    ) -> str:
        import time
        timestamp = int(time.time() * 1000)
        snapshots = []
        for p in positions:
            # Unrealized P&L = Σ (mark_price - avg_cost) * qty across YES/NO legs.
            # mark_prices maps market_id -> (yes_mid, no_mid); if absent we cannot
            # mark the position and report 0.0 rather than a fabricated value.
            unrealized = 0.0
            if mark_prices is not None:
                price = mark_prices.get(p.market_id)
                if price is not None:
                    yes_mid, no_mid = price
                    if p.avg_cost_yes is not None:
                        unrealized += p.yes_qty * (yes_mid - p.avg_cost_yes)
                    if p.avg_cost_no is not None:
                        unrealized += p.no_qty * (no_mid - p.avg_cost_no)
            snapshots.append(
                PositionSnapshot(
                    timestamp=timestamp,
                    market_id=p.market_id,
                    platform=p.platform.value,
                    yes_qty=p.yes_qty,
                    no_qty=p.no_qty,
                    avg_cost_yes=p.avg_cost_yes if p.avg_cost_yes is not None else 0.0,
                    avg_cost_no=p.avg_cost_no if p.avg_cost_no is not None else 0.0,
                    unrealized_pnl=unrealized,
                )
            )

        if output_format == "csv":
            return self._write_position_csv(snapshots, filename or "position_report.csv")
        elif output_format == "json":
            return self._write_position_json(snapshots, filename or "position_report.json")
        else:
            raise ValueError(f"Unsupported format: {output_format}")

    def _write_csv(self, entries: List[TradeReportEntry], filename: str) -> str:
        filepath = os.path.join(self._report_dir, filename)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "market_id", "platform", "side",
                "size_usdc", "fill_price", "order_id", "proposal_id"
            ])
            for e in entries:
                writer.writerow([
                    e.timestamp, e.market_id, e.platform, e.side,
                    e.size_usdc, e.fill_price, e.order_id, e.proposal_id
                ])
        return filepath

    def _write_json(self, entries: List[TradeReportEntry], filename: str) -> str:
        import json
        filepath = os.path.join(self._report_dir, filename)
        data = [
            {
                "timestamp": e.timestamp,
                "market_id": e.market_id,
                "platform": e.platform,
                "side": e.side,
                "size_usdc": e.size_usdc,
                "fill_price": e.fill_price,
                "order_id": e.order_id,
                "proposal_id": e.proposal_id,
            }
            for e in entries
        ]
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        return filepath

    def _write_position_csv(self, snapshots: List[PositionSnapshot], filename: str) -> str:
        filepath = os.path.join(self._report_dir, filename)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "market_id", "platform", "yes_qty", "no_qty",
                "avg_cost_yes", "avg_cost_no", "unrealized_pnl"
            ])
            for s in snapshots:
                writer.writerow([
                    s.timestamp, s.market_id, s.platform, s.yes_qty, s.no_qty,
                    s.avg_cost_yes, s.avg_cost_no, s.unrealized_pnl
                ])
        return filepath

    def _write_position_json(self, snapshots: List[PositionSnapshot], filename: str) -> str:
        import json
        filepath = os.path.join(self._report_dir, filename)
        data = [
            {
                "timestamp": s.timestamp,
                "market_id": s.market_id,
                "platform": s.platform,
                "yes_qty": s.yes_qty,
                "no_qty": s.no_qty,
                "avg_cost_yes": s.avg_cost_yes,
                "avg_cost_no": s.avg_cost_no,
                "unrealized_pnl": s.unrealized_pnl,
            }
            for s in snapshots
        ]
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        return filepath
