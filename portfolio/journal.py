from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import List, Optional

from portfolio.manager import FillRecord

logger = logging.getLogger(__name__)


@dataclass
class JournalEntry:
    timestamp: str
    fill_id: str
    market_id: str
    platform: str
    side: str
    size_usdc: float
    price: float
    fee_usdc: float
    realised_pnl: float
    hold_time_ms: int
    slippage_bps: Optional[int]
    strategy_id: str
    proposal_id: str


class TradeJournal:
    def __init__(self, output_dir: str = "trade_journal") -> None:
        self._output_dir = output_dir
        self._entries: List[JournalEntry] = []

    def record_fill(self, fill: FillRecord) -> None:
        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            fill_id=fill.fill_id,
            market_id=fill.market_id,
            platform=fill.platform.value if hasattr(fill.platform, "value") else str(fill.platform),
            side=fill.side.value if hasattr(fill.side, "value") else str(fill.side),
            size_usdc=fill.size_usdc,
            price=fill.price,
            fee_usdc=fill.fee_usdc,
            realised_pnl=fill.realised_pnl,
            hold_time_ms=fill.hold_time_ms,
            slippage_bps=fill.slippage_bps,
            strategy_id=fill.strategy_id.value if hasattr(fill.strategy_id, "value") else str(fill.strategy_id),
            proposal_id=fill.proposal_id,
        )
        self._entries.append(entry)

    def export_csv(self, filename: Optional[str] = None) -> str:
        if filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"trade_journal_{ts}.csv"

        os.makedirs(self._output_dir, exist_ok=True)
        filepath = os.path.join(self._output_dir, filename)

        field_names = [f.name for f in fields(JournalEntry)]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=field_names)
            writer.writeheader()
            for entry in self._entries:
                writer.writerow({fn: getattr(entry, fn) for fn in field_names})

        logger.info("Exported %d trades to %s", len(self._entries), filepath)
        return filepath

    @property
    def total_trades(self) -> int:
        return len(self._entries)

    @property
    def gross_pnl(self) -> float:
        return sum(e.realised_pnl for e in self._entries)

    @property
    def win_rate(self) -> float:
        if not self._entries:
            return 0.0
        wins = sum(1 for e in self._entries if e.realised_pnl > 0)
        return wins / len(self._entries)

    @property
    def avg_hold_time_ms(self) -> float:
        if not self._entries:
            return 0.0
        return sum(e.hold_time_ms for e in self._entries) / len(self._entries)
