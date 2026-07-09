from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WhaleProfile:
    address: str
    name: Optional[str] = None
    win_rate: Optional[float] = None
    total_volume_usdc: Optional[float] = None
    total_pnl_usdc: Optional[float] = None
    category: Optional[str] = None
    is_active: bool = True
    last_trade_ts: Optional[int] = None


@dataclass(frozen=True)
class WhaleTradeEvent:
    wallet_address: str
    market_id: str
    side: str
    size_usdc: float
    price: float
    ts: int
    tx_hash: str = ""
    whale_name: Optional[str] = None
    platform: str = "polymarket"
    token_id: Optional[str] = None
    outcome: Optional[str] = None
