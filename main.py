#!/usr/bin/env python3
"""
main.py — PMTS entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from typing import TYPE_CHECKING, Any

from config.logging_setup import configure_logging
from config.settings import Settings, get_settings
from src.clock import LiveClock
from src.enums import Platform

if TYPE_CHECKING:
    from data.market_data_provider import MarketDataProvider
    from src.protocols import PortfolioStore

logger = logging.getLogger(__name__)

DEFAULT_BACKTEST_MARKETS = ["BTC-Q4", "ETH-Q1", "SOL-Q2"]
BACKTEST_RISK_LIMITS: dict[str, Any] = {
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


def _stable_seed(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def _venue_market_ids(settings: Settings, venue: str) -> list[str]:
    registry = settings.trading.market_registry
    if not registry:
        return list(settings.trading.markets)
    return [registry[m][venue] for m in settings.trading.markets]


def _venue_token_ids(settings: Settings, token_field: str) -> list[str]:
    """Extract CLOB token IDs from registry for WS subscription."""
    registry = settings.trading.market_registry
    if not registry:
        logger.warning("No market registry configured — cannot resolve CLOB token IDs for WS subscription")
        return []
    return [registry[m].get(token_field, "") for m in settings.trading.markets if registry[m].get(token_field)]


def _token_to_logical_map(settings: Settings, token_field: str) -> dict[str, str]:
    """Map from CLOB token IDs back to logical market IDs."""
    registry = settings.trading.market_registry
    if not registry:
        return {}
    result = {}
    for m in settings.trading.markets:
        token = registry[m].get(token_field, "")
        if token:
            result[token] = m
    return result


def _pm_market_id_map(settings: Settings) -> dict[str, str]:
    """Build a market_id_map for PolymarketClient using YES token IDs.

    The PolymarketClient uses this to resolve logical market IDs to CLOB token
    IDs for order placement. We use YES tokens as the default mapping; the
    client's place_order method should be updated to select the correct token
    based on order side when both YES and NO tokens are available.
    """
    registry = settings.trading.market_registry
    if not registry:
        return {}
    result = {}
    for m in settings.trading.markets:
        yes_token = registry[m].get("pm_yes_token", "")
        if yes_token:
            result[m] = yes_token
    return result


def _logical_to_venue_map(settings: Settings, venue: str) -> dict[str, str]:
    registry = settings.trading.market_registry
    if not registry:
        return {}
    return {m: registry[m][venue] for m in settings.trading.markets}


def _venue_to_logical_map(settings: Settings, venue: str) -> dict[str, str]:
    registry = settings.trading.market_registry
    if not registry:
        return {}
    return {registry[m][venue]: m for m in settings.trading.markets}


def _market_data_metrics(mdp: MarketDataProvider) -> dict[str, Any]:
    return {
        "snapshots_received": mdp.snapshots_received,
        "stale_emitted": mdp.stale_emitted,
        "dedup_suppressed": mdp.dedup_suppressed,
        "markets_seen_total": mdp.get_total_markets_seen(),
        "markets_seen_by_platform": mdp.get_market_counts_by_platform(),
    }

async def run_live() -> None:
    from ai.enhancer import AIEnhancerConfig, AISignalEnhancer
    from data.adapters.opinion_ws import OpinionWSAdapter
    from data.adapters.polymarket_ws import PolymarketWSAdapter
    from data.market_data_provider import MarketDataProvider
    from engine.market_monitor import MarketMonitor
    from engine.orchestrator import Orchestrator
    from engine.strategy_engine import StrategyConfig, StrategyEngine
    from execution.clients.opinion import OpinionClient
    from execution.clients.polymarket import PolymarketClient
    from execution.engine import ExecutionEngine
    from infrastructure.alerting import AlertConfig as AlertCfg
    from infrastructure.alerting import AlertRouter
    from infrastructure.circuit_breaker import CircuitBreakerExchangeWrapper
    from infrastructure.observability import HealthMonitor, ObservabilityServer
    from portfolio.analytics import PortfolioAnalytics
    from portfolio.manager import PortfolioManager
    from portfolio.storage import SqlitePortfolioStore
    from portfolio.storage_postgres import PostgresPortfolioStore
    from risk.engine import RiskEngine
    from risk.kill_switch import KillSwitch
    from risk.limits import RiskLimits
    from strategies.arbitrage import ArbConfig
    from strategies.delta_neutral import DeltaNeutralConfig

    settings = get_settings()
    settings.validate(mode="live")

    logger.info("Live trading initializing: markets=%s", settings.trading.markets)

    clock = LiveClock()

    # Use PostgreSQL when DATABASE_URL is set, otherwise SQLite
    database_url = settings.trading.database_url
    store: PortfolioStore
    if database_url:
        pg_store = PostgresPortfolioStore(dsn=database_url)
        await pg_store.connect()
        store = pg_store
        logger.info("Using PostgreSQL backend")
    else:
        db_path = getattr(settings.trading, "db_path", "portfolio.db")
        store = SqlitePortfolioStore(db_path=db_path)
        logger.info("Using SQLite backend")

    alert_cfg = AlertCfg(
        slack_webhook_url=settings.alerts.slack_webhook_url or None,
        email_smtp_host=settings.alerts.email_smtp_host,
        email_smtp_port=settings.alerts.email_smtp_port,
        email_username=settings.alerts.email_username or None,
        email_password=settings.alerts.email_password or None,
        email_recipients=[r.strip() for r in settings.alerts.email_recipients.split(",") if r.strip()],
        webhook_urls=[u.strip() for u in settings.alerts.webhook_urls.split(",") if u.strip()],
    )
    alert_router = AlertRouter(alert_cfg)

    pm_client_raw = PolymarketClient(
        api_key=settings.polymarket.api_key,
        secret=settings.polymarket.api_secret,
        passphrase=settings.polymarket.passphrase,
        wallet_private_key=settings.polymarket.wallet_key,
        host=settings.polymarket.clob_url,
        sandbox=settings.polymarket.sandbox,
        market_id_map=_pm_market_id_map(settings),
    )
    op_client_raw = OpinionClient(
        api_key=settings.opinion.api_key,
        wallet_private_key=settings.opinion.wallet_key,
        ctf_exchange_addr=settings.opinion.ctf_exchange_addr,
        host=settings.opinion.rest_url,
        sandbox=settings.opinion.sandbox,
        market_id_map=_logical_to_venue_map(settings, "opinion"),
    )
    pm_client = CircuitBreakerExchangeWrapper(pm_client_raw, base_name="Polymarket")
    op_client = CircuitBreakerExchangeWrapper(op_client_raw, base_name="Opinion")
    pm_ws = PolymarketWSAdapter(
        asset_ids=_venue_token_ids(settings, "pm_yes_token"),
        ws_url=settings.polymarket.ws_url,
        taker_fee_bps=settings.polymarket.taker_fee_bps,
        market_id_map=_token_to_logical_map(settings, "pm_yes_token"),
    )
    op_ws = OpinionWSAdapter(
        market_ids=_venue_market_ids(settings, "opinion"),
        ws_url=settings.opinion.ws_url,
        taker_fee_bps=settings.opinion.taker_fee_bps,
        market_id_map=_venue_to_logical_map(settings, "opinion"),
    )

    mdp = MarketDataProvider(adapters=[pm_ws, op_ws], clock=clock)

    def price_source(market_id: str, platform: Platform) -> tuple[float, float]:
        mid = mdp.get_mid_prices(market_id, platform)
        return mid if mid is not None else (0.50, 0.50)

    analytics = PortfolioAnalytics()

    portfolio = PortfolioManager(
        initial_cash_usdc=settings.trading.initial_cash_usdc,
        price_source=price_source,
        store=store,
        clock=clock,
        fill_callback=analytics.add_fill,
    )

    risk_limits = RiskLimits(
        drawdown_kill_pct=settings.trading.drawdown_kill_pct,
        drawdown_warn_pct=settings.trading.drawdown_warn_pct,
        max_single_order_usdc=settings.trading.max_order_usdc,
        min_single_order_usdc=settings.trading.min_order_usdc,
        max_market_exposure_pct=settings.trading.max_market_exposure_pct,
        max_market_exposure_usdc=settings.trading.max_market_exposure_usdc,
        max_net_delta_per_market=settings.trading.max_net_delta,
    )

    kill_switch = KillSwitch(confirmation_token=settings.trading.kill_switch_token, alert_router=alert_router)
    risk = RiskEngine(portfolio=portfolio, kill_switch=kill_switch, limits=risk_limits, store=store, alert_router=alert_router, clock=clock)

    ai_cfg = AIEnhancerConfig(
        enabled=settings.ai.enabled,
        use_heuristic_only=settings.ai.heuristic_only,
        provider=settings.ai.provider,
        anthropic_api_key=settings.ai.anthropic_api_key,
        openrouter_api_key=settings.ai.openrouter_api_key,
        openrouter_model=settings.ai.openrouter_model,
        api_timeout_ms=settings.ai.api_timeout_ms,
        cache_ttl_ms=settings.ai.cache_ttl_ms,
    )
    ai_enhancer = AISignalEnhancer(config=ai_cfg)

    strat_cfg = StrategyConfig(
        arb_enabled=settings.trading.enable_arb,
        mm_enabled=settings.trading.enable_mm,
        hedge_enabled=settings.trading.enable_hedge,
        arb_budget_usdc=settings.trading.arb_budget_usdc,
        mm_budget_usdc=settings.trading.mm_budget_usdc,
    )
    arb_cfg = ArbConfig(
        min_net_edge=settings.trading.min_net_edge,
        max_order_usdc=settings.trading.max_order_usdc,
        min_order_usdc=settings.trading.min_order_usdc,
    )
    dn_cfg = DeltaNeutralConfig(
        hedge_threshold=settings.trading.hedge_threshold,
        mm_quote_size_usdc=settings.trading.mm_quote_size_usdc,
    )

    strategy = StrategyEngine(
        config=strat_cfg,
        arb_config=arb_cfg,
        dn_config=dn_cfg,
        ai_enhancer=ai_enhancer,
    )

    pm_engine = ExecutionEngine(pm_client, risk=risk, store=store, mdb=mdp, alert_router=alert_router, clock=clock)
    op_engine = ExecutionEngine(op_client, risk=risk, store=store, mdb=mdp, alert_router=alert_router, clock=clock)

    orchestrator = Orchestrator(
        mdp=mdp,
        portfolio=portfolio,
        risk=risk,
        strategy=strategy,
        pm_engine=pm_engine,
        op_engine=op_engine,
        markets=settings.trading.markets,
        ai_enhancer=ai_enhancer,
        enable_trading=settings.trading.enable_trading,
        clock=clock,
    )
    risk.set_kill_switch_reset_callback(orchestrator._on_kill_switch_reset)
    market_monitor = MarketMonitor(
        client=pm_client,
        orchestrator=orchestrator,
        markets=settings.trading.markets,
    )

    def handle_resolution(market_id: str, outcome: str) -> None:
        logger.info("Market %s resolved to %s, notifying orchestrator", market_id, outcome)
        asyncio.create_task(orchestrator.handle_market_resolution(market_id, outcome))

    from engine.resolution_monitor import ResolutionMonitor
    resolution_monitor = ResolutionMonitor(
        client=pm_client,
        markets=settings.trading.markets,
        on_resolution=handle_resolution,
    )

    # Market registry hot-reload
    market_registry_path = settings.trading.market_registry_path
    from infrastructure.market_watcher import MarketRegistryWatcher
    def _reload_market_registry(registry: dict[str, Any]) -> None:
        logger.info("Market registry hot-reloaded (%d entries)", len(registry))
        new_markets = list(registry.keys())
        if new_markets:
            settings.trading.markets = new_markets
            settings.trading.market_registry = registry
    market_watcher = MarketRegistryWatcher(
        file_path=market_registry_path,
        callback=_reload_market_registry,
        poll_interval_s=30.0,
    )

    obs_bind_host = settings.observability.bind_host
    obs_port = settings.observability.port
    obs_server = ObservabilityServer(port=obs_port, bind_host=obs_bind_host)

    monitor = HealthMonitor(
        mdp=mdp,
        engines=[pm_engine, op_engine],
        risk=risk,
        store=store,
        kill_switch=kill_switch,
        obs_server=obs_server,
        mode="live",
    )

    obs_server.set_health_monitor(monitor)
    obs_server.set_dashboard_sources(
        portfolio=portfolio,
        orchestrator=orchestrator,
        alert_router=alert_router,
        analytics=analytics,
    )
    obs_server.set_kill_switch_config(
        token=settings.trading.kill_switch_token,
        reset_callback=orchestrator._on_kill_switch_reset,
        activate_callback=orchestrator.emergency_stop,
    )

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
            "total_value": portfolio.get_portfolio_mtm().total_equity_usdc,
            "cash_usdc": portfolio.cash_usdc,
            "reserved_capital": risk.reserved_capital,
        },
        "market_data": _market_data_metrics(mdp),
    })

    # Graceful Shutdown
    shutdown_event = asyncio.Event()
    def handle_sig(*args: Any) -> None:
        logger.info("Received shutdown signal...")
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_sig)
    except NotImplementedError:
        pass

    # START
    await obs_server.start()

    logger.info("Performing startup reconciliation...")
    await pm_engine.reconcile()
    await op_engine.reconcile()
    risk.reconcile_reservations()

    await orchestrator.start()
    await market_monitor.start()
    await resolution_monitor.start()
    await market_watcher.start()

    async def liveness_tick_loop() -> None:
        while True:
            monitor.tick_liveness()
            await asyncio.sleep(5)

    liveness_task = asyncio.create_task(liveness_tick_loop(), name="liveness-tick")

    # Periodic order-state reconciliation against the exchange (best-effort drift detection)
    from infrastructure.reconciliation import OrderReconciler

    reconciler = OrderReconciler(
        engines=[pm_engine, op_engine],
        alert_router=alert_router,
        clock=clock,
        interval_s=settings.trading.reconcile_interval_s,
    )
    await reconciler.start()

    logger.info("SYSTEM LIVE TRADING mode.")

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down live trading...")
    liveness_task.cancel()
    await resolution_monitor.stop()
    await market_monitor.stop()
    await orchestrator.stop()
    await obs_server.stop()
    await reconciler.stop()
    await pm_client.close()
    await op_client.close()
    await alert_router.close()
    if asyncio.iscoroutinefunction(store.close):
        await store.close()
    else:
        store.close()
    logger.info("Shutdown complete.")


async def run_paper(fill_prob: float = 0.85) -> None:
    from ai.enhancer import AIEnhancerConfig, AISignalEnhancer
    from data.adapters.opinion_ws import OpinionWSAdapter
    from data.adapters.polymarket_ws import PolymarketWSAdapter
    from data.market_data_provider import MarketDataProvider
    from engine.market_monitor import MarketMonitor
    from engine.orchestrator import Orchestrator
    from engine.strategy_engine import StrategyConfig, StrategyEngine
    from execution.clients.paper import PaperTradingClient
    from execution.engine import ExecutionEngine
    from infrastructure.alerting import AlertConfig as AlertCfg
    from infrastructure.alerting import AlertRouter
    from infrastructure.observability import HealthMonitor, ObservabilityServer
    from portfolio.analytics import PortfolioAnalytics
    from portfolio.manager import PortfolioManager
    from portfolio.storage import SqlitePortfolioStore
    from portfolio.storage_postgres import PostgresPortfolioStore
    from risk.engine import RiskEngine
    from risk.kill_switch import KillSwitch
    from risk.limits import RiskLimits
    from strategies.arbitrage import ArbConfig
    from strategies.delta_neutral import DeltaNeutralConfig

    settings = get_settings()
    settings.validate(mode="paper")

    logger.info("Paper trading initializing: markets=%s", settings.trading.markets)

    # Use PostgreSQL when DATABASE_URL is set, otherwise SQLite
    database_url = settings.trading.database_url
    store: PortfolioStore
    if database_url:
        pg_store = PostgresPortfolioStore(dsn=database_url)
        await pg_store.connect()
        store = pg_store
        logger.info("Using PostgreSQL backend")
    else:
        db_path = getattr(settings.trading, "db_path", "portfolio_paper.db")
        store = SqlitePortfolioStore(db_path=db_path)
        logger.info("Using SQLite backend")

    clock = LiveClock()

    alert_cfg = AlertCfg(
        slack_webhook_url=settings.alerts.slack_webhook_url or None,
        email_smtp_host=settings.alerts.email_smtp_host,
        email_smtp_port=settings.alerts.email_smtp_port,
        email_username=settings.alerts.email_username or None,
        email_password=settings.alerts.email_password or None,
        email_recipients=[r.strip() for r in settings.alerts.email_recipients.split(",") if r.strip()],
        webhook_urls=[u.strip() for u in settings.alerts.webhook_urls.split(",") if u.strip()],
    )
    alert_router = AlertRouter(alert_cfg)

    pm_client = PaperTradingClient(fill_probability=fill_prob, seed=42)
    op_client = PaperTradingClient(fill_probability=fill_prob, seed=43)
    pm_client.PLATFORM = Platform.POLYMARKET
    op_client.PLATFORM = Platform.OPINION

    pm_ws = PolymarketWSAdapter(
        asset_ids=_venue_token_ids(settings, "pm_yes_token"),
        ws_url=settings.polymarket.ws_url,
        taker_fee_bps=settings.polymarket.taker_fee_bps,
        market_id_map=_token_to_logical_map(settings, "pm_yes_token"),
    )
    op_ws = OpinionWSAdapter(
        market_ids=_venue_market_ids(settings, "opinion"),
        ws_url=settings.opinion.ws_url,
        taker_fee_bps=settings.opinion.taker_fee_bps,
        market_id_map=_venue_to_logical_map(settings, "opinion"),
    )

    mdp = MarketDataProvider(adapters=[pm_ws, op_ws], alert_router=alert_router, clock=clock)

    def price_source(market_id: str, platform: Platform) -> tuple[float, float]:
        mid = mdp.get_mid_prices(market_id, platform)
        return mid if mid is not None else (0.50, 0.50)

    analytics = PortfolioAnalytics()

    portfolio = PortfolioManager(
        initial_cash_usdc=settings.trading.initial_cash_usdc,
        price_source=price_source,
        store=store,
        clock=clock,
        fill_callback=analytics.add_fill,
    )

    risk_limits = RiskLimits(
        drawdown_kill_pct=settings.trading.drawdown_kill_pct,
        drawdown_warn_pct=settings.trading.drawdown_warn_pct,
        max_single_order_usdc=settings.trading.max_order_usdc,
        min_single_order_usdc=settings.trading.min_order_usdc,
        max_market_exposure_pct=settings.trading.max_market_exposure_pct,
        max_market_exposure_usdc=settings.trading.max_market_exposure_usdc,
        max_net_delta_per_market=settings.trading.max_net_delta,
    )

    kill_switch = KillSwitch(confirmation_token=settings.trading.kill_switch_token, alert_router=alert_router)
    risk = RiskEngine(portfolio=portfolio, kill_switch=kill_switch, limits=risk_limits, store=store, alert_router=alert_router, clock=clock)

    ai_cfg = AIEnhancerConfig(
        enabled=settings.ai.enabled,
        use_heuristic_only=settings.ai.heuristic_only,
        provider=settings.ai.provider,
        anthropic_api_key=settings.ai.anthropic_api_key,
        openrouter_api_key=settings.ai.openrouter_api_key,
        openrouter_model=settings.ai.openrouter_model,
        api_timeout_ms=settings.ai.api_timeout_ms,
        cache_ttl_ms=settings.ai.cache_ttl_ms,
    )
    ai_enhancer = AISignalEnhancer(config=ai_cfg)

    strat_cfg = StrategyConfig(
        arb_enabled=settings.trading.enable_arb,
        mm_enabled=settings.trading.enable_mm,
        hedge_enabled=settings.trading.enable_hedge,
        arb_budget_usdc=settings.trading.arb_budget_usdc,
        mm_budget_usdc=settings.trading.mm_budget_usdc,
    )
    arb_cfg = ArbConfig(
        min_net_edge=settings.trading.min_net_edge,
        max_order_usdc=settings.trading.max_order_usdc,
        min_order_usdc=settings.trading.min_order_usdc,
    )
    dn_cfg = DeltaNeutralConfig(
        hedge_threshold=settings.trading.hedge_threshold,
        mm_quote_size_usdc=settings.trading.mm_quote_size_usdc,
    )

    strategy = StrategyEngine(
        config=strat_cfg,
        arb_config=arb_cfg,
        dn_config=dn_cfg,
        ai_enhancer=ai_enhancer,
    )

    pm_engine = ExecutionEngine(pm_client, risk=risk, store=store, mdb=mdp, alert_router=alert_router, clock=clock)
    op_engine = ExecutionEngine(op_client, risk=risk, store=store, mdb=mdp, alert_router=alert_router, clock=clock)

    orchestrator = Orchestrator(
        mdp=mdp,
        portfolio=portfolio,
        risk=risk,
        strategy=strategy,
        pm_engine=pm_engine,
        op_engine=op_engine,
        markets=settings.trading.markets,
        ai_enhancer=ai_enhancer,
        enable_trading=settings.trading.enable_trading,
        clock=clock,
        alert_router=alert_router,
    )
    risk.set_kill_switch_reset_callback(orchestrator._on_kill_switch_reset)
    market_monitor = MarketMonitor(
        client=pm_client,
        orchestrator=orchestrator,
        markets=settings.trading.markets,
    )

    def handle_resolution(market_id: str, outcome: str) -> None:
        logger.info("Market %s resolved to %s, notifying orchestrator", market_id, outcome)
        asyncio.create_task(orchestrator.handle_market_resolution(market_id, outcome))

    from engine.resolution_monitor import ResolutionMonitor
    resolution_monitor = ResolutionMonitor(
        client=pm_client,
        markets=settings.trading.markets,
        on_resolution=handle_resolution,
    )

    # Market registry hot-reload
    market_registry_path = settings.trading.market_registry_path
    from infrastructure.market_watcher import MarketRegistryWatcher
    def _reload_market_registry(registry: dict[str, Any]) -> None:
        logger.info("Market registry hot-reloaded (%d entries)", len(registry))
        new_markets = list(registry.keys())
        if new_markets:
            settings.trading.markets = new_markets
            settings.trading.market_registry = registry
    market_watcher = MarketRegistryWatcher(
        file_path=market_registry_path,
        callback=_reload_market_registry,
        poll_interval_s=30.0,
    )

    obs_bind_host = settings.observability.bind_host
    obs_port = settings.observability.port
    obs_server = ObservabilityServer(port=obs_port, bind_host=obs_bind_host)

    monitor = HealthMonitor(
        mdp=mdp,
        engines=[pm_engine, op_engine],
        risk=risk,
        store=store,
        kill_switch=kill_switch,
        obs_server=obs_server,
        mode="paper",
    )

    obs_server.set_health_monitor(monitor)
    obs_server.set_dashboard_sources(
        portfolio=portfolio,
        orchestrator=orchestrator,
        alert_router=alert_router,
        analytics=analytics,
    )
    obs_server.set_kill_switch_config(
        token=settings.trading.kill_switch_token,
        reset_callback=orchestrator._on_kill_switch_reset,
        activate_callback=orchestrator.emergency_stop,
    )

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
            "total_value": portfolio.get_portfolio_mtm().total_equity_usdc,
            "cash_usdc": portfolio.cash_usdc,
            "reserved_capital": risk.reserved_capital,
        },
        "market_data": _market_data_metrics(mdp),
    })

    shutdown_event = asyncio.Event()
    def handle_sig(*args: Any) -> None:
        logger.info("Received shutdown signal...")
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_sig)
    except Exception:
        pass

    await obs_server.start()

    logger.info("Performing startup reconciliation (paper mode)...")
    await pm_engine.reconcile()
    await op_engine.reconcile()
    risk.reconcile_reservations()

    await orchestrator.start()
    await market_monitor.start()
    await resolution_monitor.start()
    await market_watcher.start()

    async def liveness_tick_loop() -> None:
        while True:
            monitor.tick_liveness()
            await asyncio.sleep(5)

    liveness_task = asyncio.create_task(liveness_tick_loop(), name="liveness-tick")

    logger.info("SYSTEM PAPER TRADING mode. No real capital at risk.")

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down paper trading...")
    liveness_task.cancel()
    await resolution_monitor.stop()
    await market_monitor.stop()
    await orchestrator.stop()
    await obs_server.stop()
    await pm_client.close()
    await op_client.close()
    await alert_router.close()
    if asyncio.iscoroutinefunction(store.close):
        await store.close()
    else:
        store.close()
    logger.info("Paper trading shutdown complete.")


async def run_paper_offline(fill_prob: float = 0.85) -> None:
    from ai.enhancer import AIEnhancerConfig, AISignalEnhancer
    from data.adapters.synthetic import SyntheticMarketFeedAdapter
    from data.market_data_provider import MarketDataProvider
    from engine.orchestrator import Orchestrator
    from engine.strategy_engine import StrategyConfig, StrategyEngine
    from execution.clients.paper import PaperTradingClient
    from execution.engine import ExecutionEngine
    from infrastructure.alerting import AlertConfig as AlertCfg
    from infrastructure.alerting import AlertRouter
    from infrastructure.observability import HealthMonitor, ObservabilityServer
    from portfolio.analytics import PortfolioAnalytics
    from portfolio.manager import PortfolioManager
    from portfolio.storage import SqlitePortfolioStore
    from portfolio.storage_postgres import PostgresPortfolioStore
    from risk.engine import RiskEngine
    from risk.kill_switch import KillSwitch
    from risk.limits import RiskLimits
    from strategies.arbitrage import ArbConfig
    from strategies.delta_neutral import DeltaNeutralConfig

    settings = get_settings()
    settings.validate(mode="paper")

    logger.info("Offline paper trading initializing: markets=%s", settings.trading.markets)

    # Use PostgreSQL when DATABASE_URL is set, otherwise SQLite
    database_url = settings.trading.database_url
    store: PortfolioStore
    if database_url:
        pg_store = PostgresPortfolioStore(dsn=database_url)
        await pg_store.connect()
        store = pg_store
        logger.info("Using PostgreSQL backend")
    else:
        db_path = getattr(settings.trading, "db_path", "portfolio_paper.db")
        store = SqlitePortfolioStore(db_path=db_path)
        logger.info("Using SQLite backend")

    clock = LiveClock()

    alert_cfg = AlertCfg(
        slack_webhook_url=settings.alerts.slack_webhook_url or None,
        email_smtp_host=settings.alerts.email_smtp_host,
        email_smtp_port=settings.alerts.email_smtp_port,
        email_username=settings.alerts.email_username or None,
        email_password=settings.alerts.email_password or None,
        email_recipients=[r.strip() for r in settings.alerts.email_recipients.split(",") if r.strip()],
        webhook_urls=[u.strip() for u in settings.alerts.webhook_urls.split(",") if u.strip()],
    )
    alert_router = AlertRouter(alert_cfg)

    markets = settings.trading.markets or DEFAULT_BACKTEST_MARKETS
    pm_feed = SyntheticMarketFeedAdapter(
        market_ids=markets,
        platform=Platform.POLYMARKET,
        taker_fee_bps=settings.polymarket.taker_fee_bps,
        seed=42,
        base_mid=0.46,
    )
    op_feed = SyntheticMarketFeedAdapter(
        market_ids=markets,
        platform=Platform.OPINION,
        taker_fee_bps=settings.opinion.taker_fee_bps,
        seed=43,
        base_mid=0.54,
    )

    mdp = MarketDataProvider(adapters=[pm_feed, op_feed], alert_router=alert_router, clock=clock)

    def price_source(market_id: str, platform: Platform) -> tuple[float, float]:
        mid = mdp.get_mid_prices(market_id, platform)
        return mid if mid is not None else (0.50, 0.50)

    analytics = PortfolioAnalytics()

    portfolio = PortfolioManager(
        initial_cash_usdc=settings.trading.initial_cash_usdc,
        price_source=price_source,
        store=store,
        clock=clock,
        fill_callback=analytics.add_fill,
    )

    risk_limits = RiskLimits(
        drawdown_kill_pct=settings.trading.drawdown_kill_pct,
        drawdown_warn_pct=settings.trading.drawdown_warn_pct,
        max_single_order_usdc=settings.trading.max_order_usdc,
        min_single_order_usdc=settings.trading.min_order_usdc,
        max_market_exposure_pct=settings.trading.max_market_exposure_pct,
        max_market_exposure_usdc=settings.trading.max_market_exposure_usdc,
        max_net_delta_per_market=settings.trading.max_net_delta,
    )

    kill_switch = KillSwitch(confirmation_token=settings.trading.kill_switch_token, alert_router=alert_router)
    risk = RiskEngine(portfolio=portfolio, kill_switch=kill_switch, limits=risk_limits, store=store, alert_router=alert_router, clock=clock)

    ai_cfg = AIEnhancerConfig(
        enabled=settings.ai.enabled,
        use_heuristic_only=settings.ai.heuristic_only,
        provider=settings.ai.provider,
        anthropic_api_key=settings.ai.anthropic_api_key,
        openrouter_api_key=settings.ai.openrouter_api_key,
        openrouter_model=settings.ai.openrouter_model,
        api_timeout_ms=settings.ai.api_timeout_ms,
        cache_ttl_ms=settings.ai.cache_ttl_ms,
    )
    ai_enhancer = AISignalEnhancer(config=ai_cfg)

    strat_cfg = StrategyConfig(
        arb_enabled=settings.trading.enable_arb,
        mm_enabled=settings.trading.enable_mm,
        hedge_enabled=settings.trading.enable_hedge,
        arb_budget_usdc=settings.trading.arb_budget_usdc,
        mm_budget_usdc=settings.trading.mm_budget_usdc,
    )
    arb_cfg = ArbConfig(
        min_net_edge=settings.trading.min_net_edge,
        max_order_usdc=settings.trading.max_order_usdc,
        min_order_usdc=settings.trading.min_order_usdc,
    )
    dn_cfg = DeltaNeutralConfig(
        hedge_threshold=settings.trading.hedge_threshold,
        mm_quote_size_usdc=settings.trading.mm_quote_size_usdc,
    )

    strategy = StrategyEngine(
        config=strat_cfg,
        arb_config=arb_cfg,
        dn_config=dn_cfg,
        ai_enhancer=ai_enhancer,
    )

    pm_client = PaperTradingClient(fill_probability=fill_prob, seed=42)
    op_client = PaperTradingClient(fill_probability=fill_prob, seed=43)
    pm_client.PLATFORM = Platform.POLYMARKET
    op_client.PLATFORM = Platform.OPINION

    pm_engine = ExecutionEngine(pm_client, risk=risk, store=store, mdb=mdp, alert_router=alert_router, clock=clock)
    op_engine = ExecutionEngine(op_client, risk=risk, store=store, mdb=mdp, alert_router=alert_router, clock=clock)

    # Periodic order-state reconciliation against the exchange (best-effort drift detection)
    from infrastructure.reconciliation import OrderReconciler

    reconciler = OrderReconciler(
        engines=[pm_engine, op_engine],
        alert_router=alert_router,
        clock=clock,
        interval_s=settings.trading.reconcile_interval_s,
    )

    orchestrator = Orchestrator(
        mdp=mdp,
        portfolio=portfolio,
        risk=risk,
        strategy=strategy,
        pm_engine=pm_engine,
        op_engine=op_engine,
        markets=markets,
        ai_enhancer=ai_enhancer,
        enable_trading=settings.trading.enable_trading,
        clock=clock,
        alert_router=alert_router,
    )
    risk.set_kill_switch_reset_callback(orchestrator._on_kill_switch_reset)

    obs_bind_host = settings.observability.bind_host
    obs_port = settings.observability.port
    obs_server = ObservabilityServer(port=obs_port, bind_host=obs_bind_host)

    monitor = HealthMonitor(
        mdp=mdp,
        engines=[pm_engine, op_engine],
        risk=risk,
        store=store,
        kill_switch=kill_switch,
        obs_server=obs_server,
        mode="paper",
    )

    obs_server.set_health_monitor(monitor)
    obs_server.set_dashboard_sources(
        portfolio=portfolio,
        orchestrator=orchestrator,
        alert_router=alert_router,
        analytics=analytics,
    )
    obs_server.set_kill_switch_config(
        token=settings.trading.kill_switch_token,
        reset_callback=orchestrator._on_kill_switch_reset,
        activate_callback=orchestrator.emergency_stop,
    )

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
            "total_value": portfolio.get_portfolio_mtm().total_equity_usdc,
            "cash_usdc": portfolio.cash_usdc,
            "reserved_capital": risk.reserved_capital,
        },
        "market_data": _market_data_metrics(mdp),
    })

    shutdown_event = asyncio.Event()

    def handle_sig(*args: Any) -> None:
        logger.info("Received shutdown signal...")
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_sig)
    except NotImplementedError:
        pass

    await obs_server.start()

    logger.info("Performing startup reconciliation (offline paper mode)...")
    await pm_engine.reconcile()
    await op_engine.reconcile()
    risk.reconcile_reservations()

    await orchestrator.start()
    await reconciler.start()

    async def liveness_tick_loop() -> None:
        while True:
            monitor.tick_liveness()
            await asyncio.sleep(5)

    liveness_task = asyncio.create_task(liveness_tick_loop(), name="liveness-tick")

    logger.info("SYSTEM OFFLINE PAPER TRADING mode. No real capital at risk.")

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down offline paper trading...")
    liveness_task.cancel()
    await reconciler.stop()
    await orchestrator.stop()
    await obs_server.stop()
    await pm_client.close()
    await op_client.close()
    await alert_router.close()
    if asyncio.iscoroutinefunction(store.close):
        await store.close()
    else:
        store.close()
    logger.info("Offline paper trading shutdown complete.")


async def run_backtest(
    ticks: int,
    capital: float,
    pm_bias: float = 0.0,
    op_bias: float = 0.0,
) -> None:
    import time

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
            seed=_stable_seed(market_id),
            pm_bias=pm_bias,
            op_bias=op_bias,
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

    # Save to backtest result store
    from backtest.storage import BacktestResultStore
    bstore = BacktestResultStore()
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
    bstore.save_run(
        run_id=f"bt_{int(time.time())}",
        config={"ticks": ticks, "capital": capital, "markets": selected_markets},
        result=result_dict,
    )
    bstore.close()

def _run_sweep_cli(args: argparse.Namespace) -> None:
    from backtest.sweeper import BacktestSweeper, SweepConfig
    config = SweepConfig(
        min_net_edge_values=args.sweep_min_edge,
        max_order_usdc_values=args.sweep_max_order,
        drawdown_kill_pct_values=args.sweep_dd_kill,
        arb_budget_usdc_values=args.sweep_arb_budget,
        ticks=args.ticks,
        capital=args.capital,
        markets=args.sweep_markets,
    )
    sweeper = BacktestSweeper(config)
    sweeper.run()
    print(sweeper.summary_table())
    best = sweeper.best_by("sharpe_ratio")
    if best:
        print(f"\nBest params: {best.params}")
        print(f"Return: {best.result.total_return * 100:.2f}%  Sharpe: {best.result.sharpe_ratio:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PMTS — Prediction Market Trading System")
    parser.add_argument("--mode", choices=["backtest", "sweep", "live", "paper", "paper-offline"], default="backtest")
    parser.add_argument("--ticks", type=int, default=2000)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--pm-bias", type=float, default=0.0,
                        help="Persistent Polymarket mispricing bias for synthetic backtest "
                             "(e.g. -0.03 makes PM systematically cheaper to create a real arb edge)")
    parser.add_argument("--op-bias", type=float, default=0.0,
                        help="Persistent Opinion mispricing bias for synthetic backtest")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--paper-fill-prob", type=float, default=0.85, help="Fill probability for paper trading (0.0-1.0)")
    # Sweep args
    parser.add_argument("--sweep-min-edge", type=float, nargs="+", default=[0.003, 0.006, 0.01])
    parser.add_argument("--sweep-max-order", type=float, nargs="+", default=[100.0, 200.0, 400.0])
    parser.add_argument("--sweep-dd-kill", type=float, nargs="+", default=[0.15, 0.20, 0.25])
    parser.add_argument("--sweep-arb-budget", type=float, nargs="+", default=[1000.0, 2000.0, 4000.0])
    parser.add_argument("--sweep-markets", type=str, nargs="*", default=None)
    parser.add_argument("--compare", type=str, nargs="+", default=None, help="Run IDs to compare")
    parser.add_argument("--list-runs", type=int, nargs="?", const=10, default=None, help="List recent N runs")
    args = parser.parse_args()

    settings = get_settings()
    if args.verbose and args.log_level == "INFO":
        args.log_level = "DEBUG"
    configure_logging(
        level=args.log_level,
        fmt=settings.logging.fmt,
        file_path=settings.logging.file_path,
    )

    if args.list_runs is not None or args.compare:
        from backtest.storage import BacktestResultStore
        store = BacktestResultStore()
        if args.list_runs is not None:
            records = store.get_recent_runs(args.list_runs)
            print(f"{'Run ID':<20} {'Return':>8} {'Sharpe':>8} {'Max DD':>8} {'Ticks':>6}")
            print("-" * 60)
            for r in records:
                sr = f"{r.sharpe:.2f}" if r.sharpe is not None else "N/A"
                print(f"{r.run_id:<20} {r.total_return_pct:>7.2f}% {sr:>8} {r.max_drawdown_pct:>7.2f}% {r.total_ticks:>6}")
        if args.compare:
            print(store.compare_runs(args.compare))
        store.close()
        return

    if args.mode == "backtest":
        asyncio.run(run_backtest(args.ticks, args.capital, pm_bias=args.pm_bias, op_bias=args.op_bias))
    elif args.mode == "sweep":
        _run_sweep_cli(args)
    elif args.mode == "paper":
        try:
            settings.validate(mode="paper")
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)
        asyncio.run(run_paper(fill_prob=args.paper_fill_prob))
    elif args.mode == "paper-offline":
        try:
            settings.validate(mode="paper")
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)
        asyncio.run(run_paper_offline(fill_prob=args.paper_fill_prob))
    else:
        try:
            settings.validate(mode="live")
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)
        asyncio.run(run_live())

if __name__ == "__main__":
    main()
