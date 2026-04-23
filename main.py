#!/usr/bin/env python3
"""
main.py — PMTS entry point.

Usage:
  python main.py --mode backtest --ticks 2000 --capital 10000 --verbose
  python main.py --mode live      (requires exchange credentials in .env)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import get_settings
from config.logging_setup import configure_logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Backtest mode
# ─────────────────────────────────────────────────────────────────────────────

async def run_backtest(
    n_ticks:  int   = 2_000,
    capital:  float = 10_000.0,
    seed:     int   = 42,
    verbose:  bool  = False,
) -> None:
    from backtest.engine import (
        BacktestEngine, LatencyModel, build_synthetic_tick_stream,
    )
    from engine.strategy_engine import StrategyConfig
    from strategies.arbitrage import ArbConfig
    from strategies.delta_neutral import DeltaNeutralConfig
    from risk.limits import RiskLimits

    markets   = ["BTC-Q4", "ETH-Q1", "SOL-Q2"]
    per_mkt   = max(100, n_ticks // len(markets))
    tick_streams = {}

    for i, market in enumerate(markets):
        tick_streams[market] = build_synthetic_tick_stream(
            market_id=market,
            n_ticks=per_mkt,
            start_ts_ms=i * 1_000,
            tick_interval_ms=500,
            initial_pm_mid=0.40 + i * 0.05,
            initial_op_mid=0.60 - i * 0.05,
            vol=0.005,
            spread=0.012,
            seed=seed + i,
        )

    max_order = min(200.0, capital * 0.02)
    engine = BacktestEngine(
        tick_streams=tick_streams,
        initial_capital=capital,
        latency_model=LatencyModel(
            tick_to_signal_mean=25.0,  tick_to_signal_std=8.0,
            signal_to_submit_mean=45.0, signal_to_submit_std=12.0,
            submit_to_fill_mean=70.0,  submit_to_fill_std=20.0,
        ),
        strategy_config=StrategyConfig(
            arb_enabled=True, mm_enabled=True, hedge_enabled=True,
            arb_budget_usdc=capital * 0.20,
            mm_budget_usdc=capital * 0.30,
            arb_cooldown_ms=2_000,
            mm_cooldown_ms=500,
        ),
        arb_config=ArbConfig(
            min_net_edge=0.006,
            max_order_usdc=max_order,
            min_order_usdc=5.0,
            fill_certainty=0.65,
        ),
        dn_config=DeltaNeutralConfig(
            hedge_threshold=10.0,
            min_days_to_resolution=3.0,
        ),
        risk_limits=RiskLimits(
            drawdown_kill_pct=0.20,
            drawdown_warn_pct=0.15,
            max_single_order_usdc=max_order,
            min_single_order_usdc=1.0,
            # Market exposure: 10% of capital per market
            max_market_exposure_usdc=capital * 0.10,
            max_market_exposure_pct=0.10,
            # Delta limit: allow up to 5× max_order / min_expected_price (0.05)
            # = 5 * 200 / 0.05 = 20000 tokens (generous; risk comes from drawdown, not delta)
            max_net_delta_per_market=max_order / 0.05 * 5,
            delta_hedge_threshold=max_order / 0.05,   # start hedging at 1× max order
            max_arb_capital_usdc=capital * 0.20,
            max_mm_capital_usdc=capital * 0.30,
        ),
        seed=seed,
    )

    logger.info(
        "Backtest starting: %d markets × %d ticks, capital=$%.2f, seed=%d",
        len(markets), per_mkt, capital, seed,
    )

    result = await engine.run()

    print()
    print(result.summary())
    print()

    if verbose and result.trades:
        filled = [t for t in result.trades if t.fill_ratio > 0]
        print(f"Sample fills ({min(10, len(filled))} of {len(filled)}):")
        for t in filled[:10]:
            lat = f"{t.latency_ms}ms" if t.latency_ms is not None else "n/a"
            print(
                f"  [{t.strategy_id:5s}] {t.market_id:8s} {t.side:8s} "
                f"${t.filled_usdc:6.1f}/${t.requested_usdc:6.1f} "
                f"@ {t.fill_price:.3f} ratio={t.fill_ratio:.0%} "
                f"slip={t.slippage_bps or 0}bps lat={lat}"
            )
        print()

    logger.info("Backtest complete. Return: %+.2f%%", result.total_return * 100)


# ─────────────────────────────────────────────────────────────────────────────
# Live mode stub (requires exchange client implementations)
# ─────────────────────────────────────────────────────────────────────────────

async def run_live() -> None:
    settings = get_settings()

    if not settings.trading.markets:
        logger.error("No markets configured. Set MARKETS env var.")
        sys.exit(1)

    if settings.trading.kill_switch_token in ("CHANGE-ME", "CHANGE-ME-GENERATE-WITH-OPENSSL", ""):
        logger.error("KILL_SWITCH_TOKEN not set. Refusing to start live trading.")
        logger.error("Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
        sys.exit(1)

    logger.info(
        "Live trading starting: markets=%s capital=$%.2f",
        settings.trading.markets,
        settings.trading.initial_cash_usdc,
    )
    
    # Instantiate Exchange Clients (Structured, awaiting implementation)
    from execution.clients.polymarket import PolymarketClient
    from execution.clients.opinion import OpinionClient
    from execution.engine import ExecutionEngine
    
    pm_client = PolymarketClient(
        api_key=settings.polymarket.api_key,
        secret=settings.polymarket.api_secret,
        passphrase=settings.polymarket.passphrase,
        wallet_private_key=settings.polymarket.wallet_key,
        host=settings.polymarket.clob_url,
    )
    
    op_client = OpinionClient(
        api_key=settings.opinion.api_key,
        host=settings.opinion.rest_url,
    )
    
    pm_engine = ExecutionEngine(pm_client)
    op_engine = ExecutionEngine(op_client)
    
    # In a full live setup, you would now pass these engines into the Orchestrator
    # and start the asyncio loop. Since the clients throw NotImplementedError,
    # we exit gracefully after verifying initialization.
    logger.info("Exchange client skeletons instantiated successfully. Ready for implementation.")
    # sys.exit(1) # Removed the old hard exit


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PMTS — Prediction Market Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode backtest --ticks 2000 --capital 10000 --verbose
  python main.py --mode backtest --ticks 5000 --seed 99
  python main.py --mode live   (requires .env credentials)
        """,
    )
    parser.add_argument(
        "--mode", choices=["backtest", "live"], default="backtest",
        help="Run mode (default: backtest)",
    )
    parser.add_argument("--ticks",   type=int,   default=2_000,    help="Ticks per market")
    parser.add_argument("--capital", type=float, default=10_000.0, help="Initial capital (USDC)")
    parser.add_argument("--seed",    type=int,   default=42,        help="Random seed")
    parser.add_argument("--verbose", action="store_true",           help="Print sample fills")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(
        level=args.log_level,
        fmt=settings.logging.fmt,
        file_path=settings.logging.file_path,
    )

    if args.mode == "backtest":
        asyncio.run(run_backtest(
            n_ticks=args.ticks,
            capital=args.capital,
            seed=args.seed,
            verbose=args.verbose,
        ))
    else:
        asyncio.run(run_live())


if __name__ == "__main__":
    main()