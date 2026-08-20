from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class TimeWindow:
    start: time
    end: time

    def contains(self, dt: datetime) -> bool:
        t = dt.time()
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end


@dataclass
class MarketSchedule:
    market_id: str
    windows: list[TimeWindow] = field(default_factory=list)
    allow_weekends: bool = False
    enabled: bool = True

    def can_trade(self, dt: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        dt = dt or datetime.now(timezone.utc)
        if not self.allow_weekends and dt.weekday() >= 5:
            return False
        if not self.windows:
            return True
        return any(w.contains(dt) for w in self.windows)


DEFAULT_WINDOWS = [
    TimeWindow(start=time(8, 0), end=time(20, 0)),
]


class TradingSchedule:
    def __init__(self) -> None:
        self._schedules: Dict[str, MarketSchedule] = {}

    def configure_market(
        self,
        market_id: str,
        windows: Optional[list[TimeWindow]] = None,
        allow_weekends: bool = False,
        enabled: bool = True,
    ) -> None:
        self._schedules[market_id] = MarketSchedule(
            market_id=market_id,
            windows=windows or DEFAULT_WINDOWS,
            allow_weekends=allow_weekends,
            enabled=enabled,
        )

    def can_trade_market(self, market_id: str, dt: Optional[datetime] = None) -> bool:
        schedule = self._schedules.get(market_id)
        if schedule is None:
            return True
        return schedule.can_trade(dt)

    def can_trade_any(self, dt: Optional[datetime] = None) -> bool:
        if not self._schedules:
            return True
        dt = dt or datetime.now(timezone.utc)
        return any(s.can_trade(dt) for s in self._schedules.values())

    def markets_allowed_now(self, dt: Optional[datetime] = None) -> list[str]:
        dt = dt or datetime.now(timezone.utc)
        if not self._schedules:
            return []
        return [mid for mid, s in self._schedules.items() if s.can_trade(dt)]

    def markets_blocked_now(self, dt: Optional[datetime] = None) -> list[str]:
        dt = dt or datetime.now(timezone.utc)
        if not self._schedules:
            return []
        return [mid for mid, s in self._schedules.items() if not s.can_trade(dt)]
