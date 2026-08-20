from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backtest.engine import BacktestEngine, BacktestResult, build_synthetic_tick_stream
from backtest.storage import BacktestResultStore
from risk.limits import RiskLimits

logger = logging.getLogger(__name__)


@dataclass
class SweepConfig:
    min_net_edge_values: List[float] = field(default_factory=lambda: [0.003, 0.006, 0.01])
    max_order_usdc_values: List[float] = field(default_factory=lambda: [100.0, 200.0, 400.0])
    drawdown_kill_pct_values: List[float] = field(default_factory=lambda: [0.15, 0.20, 0.25])
    arb_budget_usdc_values: List[float] = field(default_factory=lambda: [1000.0, 2000.0, 4000.0])
    ticks: int = 2000
    capital: float = 10000.0
    markets: Optional[List[str]] = None


@dataclass
class SweepResult:
    params: Dict[str, Any]
    result: BacktestResult
    run_id: str


class BacktestSweeper:
    def __init__(
        self,
        config: SweepConfig,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self._config = config
        self._progress = progress_callback
        self._results: List[SweepResult] = []

    def run(self) -> List[SweepResult]:
        keys = ["min_net_edge", "max_order_usdc", "drawdown_kill_pct", "arb_budget_usdc"]
        values = [
            self._config.min_net_edge_values,
            self._config.max_order_usdc_values,
            self._config.drawdown_kill_pct_values,
            self._config.arb_budget_usdc_values,
        ]

        combinations = list(itertools.product(*values))
        total = len(combinations)
        logger.info("Backtest sweep: %d combinations across %d parameters", total, len(keys))

        store = BacktestResultStore()

        for i, combo in enumerate(combinations):
            params = dict(zip(keys, combo))
            run_id = f"sweep_{int(time.time())}_{i:04d}"

            try:
                result = self._run_single(params, run_id)
                sr = SweepResult(params=params, result=result, run_id=run_id)
                self._results.append(sr)

                result_dict = {
                    "total_return": result.total_return,
                    "total_pnl": result.total_pnl,
                    "sharpe_ratio": result.sharpe_ratio,
                    "max_drawdown": result.max_drawdown,
                    "total_ticks": result.total_ticks,
                    "market_ids": result.market_ids,
                    "fill_rate": result.fill_rate,
                    "total_proposals": result.total_proposals,
                    "approved_count": result.approved_count,
                }
                store.save_run(run_id, config=params, result=result_dict)

                logger.info(
                    "  [%d/%d] edge=%.3f order=$%.0f dd=%.2f budget=$%.0f → return=%.2f%% sharpe=%s",
                    i + 1, total,
                    combo[0], combo[1], combo[2], combo[3],
                    result.total_return * 100,
                    f"{result.sharpe_ratio:.2f}" if result.sharpe_ratio is not None else "N/A",
                )
            except Exception as exc:
                logger.error("  [%d/%d] Sweep combination %s failed: %s", i + 1, total, params, exc)

            if self._progress:
                self._progress(i + 1, total)

        store.close()

        best = self.best_by("sharpe_ratio")
        if best:
            logger.info("Sweep complete. Best result: sharpe=%.2f, params=%s",
                        best.result.sharpe_ratio or 0, best.params)

        return self._results

    def _run_single(self, params: Dict[str, Any], run_id: str) -> BacktestResult:

        market_ids = self._config.markets or ["btc_prediction", "eth_prediction"]
        selected_markets = market_ids[:2]
        streams = {
            mid: build_synthetic_tick_stream(mid, n_ticks=self._config.ticks // len(selected_markets), seed=hash(mid) % (2**31))
            for mid in selected_markets
        }

        engine = BacktestEngine(
            tick_streams=streams,
            initial_capital=self._config.capital,
            risk_limits=RiskLimits(
                drawdown_kill_pct=params["drawdown_kill_pct"],
                drawdown_warn_pct=params["drawdown_kill_pct"] * 0.75,
                max_single_order_usdc=params["max_order_usdc"],
                min_single_order_usdc=5.0,
            ),
            seed=42,
        )

        import asyncio
        result = asyncio.run(engine.run())
        return result

    def best_by(self, metric: str = "sharpe_ratio") -> Optional[SweepResult]:
        valid = [r for r in self._results if getattr(r.result, metric, None) is not None]
        if not valid:
            return None
        return max(valid, key=lambda r: getattr(r.result, metric))

    def summary_table(self) -> str:
        lines = ["=== BACKTEST SWEEP RESULTS ===", ""]
        header = f"{'Run':<8} {'Edge':>7} {'Order $':>8} {'DD Kill':>8} {'Budget':>8} {'Return':>8} {'Sharpe':>8} {'Max DD':>8}"
        lines.append(header)
        lines.append("-" * len(header))
        for i, sr in enumerate(self._results):
            r = sr.result
            sr_str = f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio is not None else "N/A"
            lines.append(
                f"{i:<8} {sr.params['min_net_edge']:>7.3f} {sr.params['max_order_usdc']:>8.0f} "
                f"{sr.params['drawdown_kill_pct']:>8.0%} {sr.params['arb_budget_usdc']:>8.0f} "
                f"{r.total_return * 100:>7.2f}% {sr_str:>8} {r.max_drawdown * 100:>7.2f}%"
            )
        best = self.best_by("sharpe_ratio")
        if best:
            lines.append("")
            lines.append(f"Best by Sharpe: {best.params}")
        return "\n".join(lines)
