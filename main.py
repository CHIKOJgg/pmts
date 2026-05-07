#!/usr/bin/env python3
"""
main.py — PMTS entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import signal

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import get_settings
from config.logging_setup import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_BACKTEST_MARKETS = ["BTC-Q4", "ETH-Q1", "SOL-Q2"]
BACKTEST_RISK_LIMITS = {
    "drawdown_kill_pct": 0.20,
    "drawdown_warn_pct": 0.15,
    "max_market_exposure_pct": 1.0,
    "max_market_exposure_usdc": 10_000.0,
    "max_arb_capital_usdc": 10_000.0,
    "max_mm_capital_usdc": 10_000.0,
    "max_net_delta_per_market": 10_000.0,
    "max_single_order_usdc": 200.0,
    "min_single_order_usdc": 1.0,
    "min_free_capital_pct": 0.0,
}

async def run_live() -> None:
    from ai.enhancer import AISignalEnhancer, AIEnhancerConfig
    from data.adapters.opinion_ws import OpinionWSAdapter
    from data.adapters.polymarket_ws import PolymarketWSAdapter
    from data.market_data_provider import MarketDataProvider
    from engine.orchestrator import Orchestrator
    from engine.strategy_engine import StrategyEngine, StrategyConfig
    from execution.clients.opinion import OpinionClient
    from execution.clients.polymarket import PolymarketClient
    from execution.engine import ExecutionEngine
    from infrastructure.observability import HealthMonitor, ObservabilityServer
    from portfolio.manager import PortfolioManager
    from portfolio.storage import SqlitePortfolioStore
    from risk.engine import RiskEngine
    from risk.kill_switch import KillSwitch
    from risk.limits import RiskLimits
    from strategies.arbitrage import ArbConfig
    from strategies.delta_neutral import DeltaNeutralConfig

    settings = get_settings()
    settings.validate()

    logger.info("Live trading initializing: markets=%s", settings.trading.markets)
    
    # 1. State Persistence
    db_path = getattr(settings.trading, "db_path", "portfolio.db")
    store = SqlitePortfolioStore(db_path=db_path)
    
    # 2. Portfolio & Risk
    portfolio = PortfolioManager(initial_cash_usdc=settings.trading.initial_cash_usdc)
    
    risk_limits = RiskLimits(
        drawdown_kill_pct=settings.trading.drawdown_kill_pct,
        drawdown_warn_pct=settings.trading.drawdown_warn_pct,
        max_single_order_usdc=settings.trading.max_order_usdc,
        min_single_order_usdc=settings.trading.min_order_usdc,
        max_market_exposure_pct=settings.trading.max_market_exposure_pct,
        max_market_exposure_usdc=settings.trading.max_market_exposure_usdc,
        max_net_delta_per_market=settings.trading.max_net_delta,
    )
    
    kill_switch = KillSwitch(confirmation_token=settings.trading.kill_switch_token)
    risk = RiskEngine(portfolio=portfolio, kill_switch=kill_switch, limits=risk_limits, store=store)
    
    # 3. Exchange Clients & Engines
    pm_client = PolymarketClient(
        api_key=settings.polymarket.api_key,
        secret=settings.polymarket.api_secret,
        passphrase=settings.polymarket.passphrase,
        wallet_private_key=settings.polymarket.wallet_key,
        host=settings.polymarket.clob_url,
        sandbox=settings.polymarket.sandbox,
    )
    op_client = OpinionClient(
        api_key=settings.opinion.api_key,
        wallet_private_key=settings.opinion.wallet_key,
        host=settings.opinion.rest_url,
        sandbox=settings.opinion.sandbox,
    )
    
    pm_engine = ExecutionEngine(pm_client, risk=risk, store=store)
    op_engine = ExecutionEngine(op_client, risk=risk, store=store)
    
    # 4. Strategy Engine
    strat_cfg = StrategyConfig(
        arb_enabled=settings.trading.enable_arb,
        mm_enabled=settings.trading.enable_mm,
        hedge_enabled=settings.trading.enable_hedge,
        arb_budget_usdc=settings.trading.arb_budget_usdc,
        mm_budget_usdc=settings.trading.mm_budget_usdc,
    )
    arb_cfg = ArbConfig(
        min_net_edge=0.006, 
        max_order_usdc=settings.trading.max_order_usdc,
        min_order_usdc=settings.trading.min_order_usdc
    )
    dn_cfg = DeltaNeutralConfig(
        hedge_threshold=10.0,
        mm_quote_size_usdc=25.0
    )
    
    strategy = StrategyEngine(
        config=strat_cfg,
        arb_config=arb_cfg,
        dn_config=dn_cfg
    )
    
    # 5. Data Adapters & Provider
    pm_ws = PolymarketWSAdapter(
        asset_ids=settings.trading.markets,
        ws_url=settings.polymarket.ws_url,
        taker_fee_bps=settings.polymarket.taker_fee_bps
    )
    op_ws = OpinionWSAdapter(
        market_ids=settings.trading.markets,
        ws_url=settings.opinion.ws_url,
        taker_fee_bps=settings.opinion.taker_fee_bps
    )
    
    mdp = MarketDataProvider(adapters=[pm_ws, op_ws])
    
    # 5.5 AI Signal Enhancer
    ai_cfg = AIEnhancerConfig(
        enabled=settings.ai.enabled,
        use_heuristic_only=settings.ai.heuristic_only,
        api_timeout_ms=settings.ai.api_timeout_ms,
        cache_ttl_ms=settings.ai.cache_ttl_ms
    )
    ai_enhancer = AISignalEnhancer(config=ai_cfg)
    
    # 6. Orchestrator
    orchestrator = Orchestrator(
        mdp=mdp,
        portfolio=portfolio,
        risk=risk,
        strategy=strategy,
        pm_engine=pm_engine,
        op_engine=op_engine,
        markets=settings.trading.markets,
        ai_enhancer=ai_enhancer,
        enable_trading=settings.trading.enable_trading
    )
    
    obs_server = ObservabilityServer(port=8080)
    
    # 7. Health & Observability
    monitor = HealthMonitor(
        mdp=mdp,
        engines=[pm_engine, op_engine],
        risk=risk,
        store=store,
        kill_switch=kill_switch,
        obs_server=obs_server
    )
    
    obs_server.set_health_monitor(monitor)
    
    obs_server.register_provider(lambda: {
        "orchestrator": {
            "proposals_evaluated": orchestrator.proposals_evaluated,
            "proposals_approved": orchestrator.proposals_approved,
            "proposals_rejected": orchestrator.proposals_rejected,
        },
        "risk": {
            "kill_switch_active": risk.kill_switch_active,
            "drawdown": risk.current_drawdown
        },
        "portfolio": {
            "total_value": portfolio.get_portfolio_mtm(lambda m, p: mdp.get_mid_prices(m, p)[0] if mdp.get_mid_prices(m, p) else 0.5)
        }
    })

    # Graceful Shutdown
    shutdown_event = asyncio.Event()
    def handle_sig(*args):
        logger.info("Received shutdown signal...")
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_sig)
    except Exception:
        pass

    # START
    await obs_server.start()
    
    # Step 6: Reconciliation (Issue #3)
    logger.info("Performing startup reconciliation...")
    await pm_engine.reconcile()
    await op_engine.reconcile()
    risk.reconcile_reservations()
    
    await orchestrator.start()
    
    # Liveness background task
    async def liveness_tick_loop():
        while True:
            monitor.tick_liveness()
            await asyncio.sleep(5)
    
    liveness_task = asyncio.create_task(liveness_tick_loop(), name="liveness-tick")
    
    logger.info("SYSTEM LIVE and trading.")
    
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    
    logger.info("Shutting down...")
    liveness_task.cancel()
    await orchestrator.stop()
    await obs_server.stop()
    await pm_client.close()
    await op_client.close()
    logger.info("Shutdown complete.")


async def run_backtest(ticks: int, capital: float) -> None:
    from backtest.engine import BacktestEngine, build_synthetic_tick_stream
    from risk.limits import RiskLimits

    settings = get_settings()
    market_ids = settings.trading.markets or DEFAULT_BACKTEST_MARKETS
    selected_markets = market_ids[: max(1, min(len(market_ids), ticks))]
    market_count = max(1, len(selected_markets))
    per_market_ticks = max(1, ticks // market_count)

    streams = {
        market_id: build_synthetic_tick_stream(
            market_id,
            n_ticks=per_market_ticks,
            seed=42,
        )
        for market_id in selected_markets
    }

    engine = BacktestEngine(
        tick_streams=streams,
        initial_capital=capital,
        risk_limits=RiskLimits(**BACKTEST_RISK_LIMITS),
        seed=42,
    )
    result = await engine.run()
    print(result.summary())

def main() -> None:
    parser = argparse.ArgumentParser(description="PMTS — Prediction Market Trading System")
    parser.add_argument("--mode", choices=["backtest", "live"], default="backtest")
    parser.add_argument("--ticks", type=int, default=2000)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if args.verbose and args.log_level == "INFO":
        args.log_level = "DEBUG"
    configure_logging(
        level=args.log_level,
        fmt=settings.logging.fmt,
        file_path=settings.logging.file_path,
    )

    if args.mode == "backtest":
        asyncio.run(run_backtest(args.ticks, args.capital))
    else:
        try:
            settings.validate()
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)
        asyncio.run(run_live())

if __name__ == "__main__":
    main()
