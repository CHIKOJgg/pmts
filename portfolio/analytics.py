"""portfolio/analytics.py — Performance analytics and attribution."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from portfolio.manager import FillRecord


@dataclass
class PerformanceMetrics:
    total_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    total_trades: int
    avg_hold_time_ms: float


class PortfolioAnalytics:
    def __init__(self) -> None:
        self._fills: List[FillRecord] = []
        self._equity_curve: List[float] = []

    def add_fill(self, fill: FillRecord) -> None:
        self._fills.append(fill)

    def add_equity_point(self, equity: float) -> None:
        self._equity_curve.append(equity)

    def compute_metrics(self, initial_capital: float) -> PerformanceMetrics:
        if not self._equity_curve:
            return self._empty_metrics()

        returns = self._compute_returns()
        total_return = (self._equity_curve[-1] - initial_capital) / initial_capital

        sharpe = self._sharpe_ratio(returns)
        sortino = self._sortino_ratio(returns)
        max_dd = self._max_drawdown()
        calmar = total_return / max_dd if max_dd > 0 else 0.0

        wins, losses = self._categorize_trades()
        win_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses)) / len(losses) if losses else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        avg_hold = self._avg_hold_time_ms()

        return PerformanceMetrics(
            total_return_pct=total_return * 100,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_dd * 100,
            win_rate=win_rate * 100,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            total_trades=len(wins) + len(losses),
            avg_hold_time_ms=avg_hold,
        )

    def _compute_returns(self) -> List[float]:
        if len(self._equity_curve) < 2:
            return []
        returns = []
        for i in range(1, len(self._equity_curve)):
            prev = self._equity_curve[i - 1]
            curr = self._equity_curve[i]
            if prev > 0:
                returns.append((curr - prev) / prev)
        return returns

    def _sharpe_ratio(self, returns: List[float], risk_free_rate: float = 0.0) -> float:
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        std = math.sqrt(variance)
        if std < 1e-9:
            return 0.0
        return (mean - risk_free_rate) / std * math.sqrt(252 * 24 * 60)

    def _sortino_ratio(self, returns: List[float], risk_free_rate: float = 0.0) -> float:
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return 0.0
        downside_std = math.sqrt(sum(r ** 2 for r in downside) / len(downside))
        if downside_std < 1e-9:
            return 0.0
        return (mean - risk_free_rate) / downside_std * math.sqrt(252 * 24 * 60)

    def _max_drawdown(self) -> float:
        if not self._equity_curve:
            return 0.0
        peak = self._equity_curve[0]
        max_dd = 0.0
        for equity in self._equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _categorize_trades(self) -> Tuple[List[float], List[float]]:
        wins: List[float] = []
        losses: List[float] = []
        for fill in self._fills:
            pnl = fill.filled_usdc
            if pnl > 0:
                wins.append(pnl)
            elif pnl < 0:
                losses.append(pnl)
        return wins, losses

    def _avg_hold_time_ms(self) -> float:
        if not self._fills:
            return 0.0
        total = sum(f.hold_time_ms for f in self._fills if hasattr(f, "hold_time_ms") and f.hold_time_ms > 0)
        return total / len(self._fills)

    def _empty_metrics(self) -> PerformanceMetrics:
        return PerformanceMetrics(
            total_return_pct=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            calmar_ratio=0.0,
            max_drawdown_pct=0.0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            profit_factor=0.0,
            total_trades=0,
            avg_hold_time_ms=0.0,
        )

    def get_equity_curve(self) -> List[float]:
        return list(self._equity_curve)

    def get_trade_history(self) -> List[FillRecord]:
        return list(self._fills)
