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

from data.market_data_provider import MarketDataProvider
from data.adapters.polymarket_ws import PolymarketWSAdapter
from data.adapters.opinion_ws import OpinionWSAdapter
from portfolio.manager import PortfolioManager
from portfolio.storage import SqlitePortfolioStore
from risk.engine import RiskEngine
from risk.kill_switch import KillSwitch
from risk.limits import RiskLimits
from engine.strategy_engine import StrategyEngine, StrategyConfig
from strategies.arbitrage import ArbConfig
from strategies.delta_neutral import DeltaNeutralConfig
from execution.clients.polymarket import PolymarketClient
from execution.clients.opinion import OpinionClient
from execution.engine import ExecutionEngine
from engine.orchestrator import Orchestrator
from infrastructure.observability import ObservabilityServer

logger = logging.getLogger(__name__)

async def run_live() -> None:
    settings = get_settings()
    settings.validate()

    if not settings.trading.markets:
        logger.error("No markets configured. Set MARKETS env var.")
        sys.exit(1)

    if settings.trading.kill_switch_token in ("CHANGE-ME", "CHANGE-ME-USE-A-SECURE-RANDOM-STRING", ""):
        logger.error("KILL_SWITCH_TOKEN not set correctly. Refusing to start.")
        sys.exit(1)

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
    )
    op_client = OpinionClient(
        api_key=settings.opinion.api_key,
        wallet_private_key=settings.opinion.wallet_key,
        host=settings.opinion.rest_url,
    )
    
    pm_engine = ExecutionEngine(pm_client)
    op_engine = ExecutionEngine(op_client)
    
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
    
    # 6. Orchestrator
    orchestrator = Orchestrator(
        mdp=mdp,
        portfolio=portfolio,
        risk=risk,
        strategy=strategy,
        pm_engine=pm_engine,
        op_engine=op_engine,
        markets=settings.trading.markets,
        enable_trading=settings.trading.enable_trading
    )
    
    # 7. Observability
    obs_server = ObservabilityServer(port=8080)
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
    await orchestrator.start()
    
    logger.info("SYSTEM LIVE and trading.")
    
    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    
    logger.info("Shutting down...")
    await orchestrator.stop()
    await obs_server.stop()
    await pm_client.close()
    await op_client.close()
    logger.info("Shutdown complete.")

def main() -> None:
    parser = argparse.ArgumentParser(description="PMTS — Prediction Market Trading System")
    parser.add_argument("--mode", choices=["backtest", "live"], default="backtest")
    parser.add_argument("--ticks", type=int, default=2000)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(level=args.log_level)
    
    try:
        settings.validate()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    if args.mode == "backtest":
        logger.error("Backtest mode not implemented in this entry point.")
        sys.exit(1)
    else:
        asyncio.run(run_live())

if __name__ == "__main__":
    main()