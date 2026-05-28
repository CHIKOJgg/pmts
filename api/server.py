"""api/server.py — FastAPI server for web dashboard and external integrations."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, HTTPException, Query
    from pydantic import BaseModel
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    active_strategies: int
    kill_switch: bool


class PositionResponse(BaseModel):
    market_id: str
    platform: str
    yes_qty: float
    no_qty: float
    avg_cost_yes: float
    avg_cost_no: float
    unrealized_pnl: float


class TradeResponse(BaseModel):
    timestamp: int
    market_id: str
    platform: str
    side: str
    size_usdc: float
    fill_price: float


class MetricsResponse(BaseModel):
    total_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate: float
    total_trades: int


class AlertResponse(BaseModel):
    severity: str
    title: str
    message: str
    timestamp: int


def create_app(
    health_monitor=None,
    portfolio_manager=None,
    analytics=None,
    alert_router=None,
    market_ids: Optional[List[str]] = None,
    start_time: Optional[float] = None,
) -> Any:
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI not available. API server disabled.")
        return None

    app = FastAPI(title="PMTS API", version="1.0.0")

    @app.get("/health", response_model=HealthResponse)
    async def get_health():
        if health_monitor is None:
            raise HTTPException(status_code=503, detail="Health monitor not available")
        readiness = await health_monitor.check_readiness()
        uptime = time.time() - start_time if start_time else 0.0
        return HealthResponse(
            status="healthy" if readiness.get("ready") else "unhealthy",
            uptime_seconds=uptime,
            active_strategies=readiness.get("active_strategies", 0),
            kill_switch=getattr(health_monitor.risk, "kill_switch_active", False),
        )

    @app.get("/positions", response_model=List[PositionResponse])
    async def get_positions():
        if portfolio_manager is None:
            raise HTTPException(status_code=503, detail="Portfolio manager not available")
        positions = portfolio_manager.get_all_positions()
        return [
            PositionResponse(
                market_id=p.market_id,
                platform="polymarket" if p.yes_holdings_pm > 0 or p.no_holdings_pm > 0 else "opinion",
                yes_qty=p.yes_holdings_pm + p.yes_holdings_op,
                no_qty=p.no_holdings_pm + p.no_holdings_op,
                avg_cost_yes=0.0,
                avg_cost_no=0.0,
                unrealized_pnl=0.0,
            )
            for p in positions
        ]

    @app.get("/metrics", response_model=MetricsResponse)
    async def get_metrics():
        if analytics is None:
            raise HTTPException(status_code=503, detail="Analytics not available")
        metrics = analytics.compute_metrics(portfolio_manager.cash_usdc if portfolio_manager else 0)
        return MetricsResponse(
            total_return_pct=metrics.total_return_pct,
            sharpe_ratio=metrics.sharpe_ratio,
            max_drawdown_pct=metrics.max_drawdown_pct,
            win_rate=metrics.win_rate,
            total_trades=metrics.total_trades,
        )

    @app.get("/alerts", response_model=List[AlertResponse])
    async def get_alerts(limit: int = Query(50, le=200)):
        if alert_router is None:
            raise HTTPException(status_code=503, detail="Alert router not available")
        recent = alert_router.get_recent(limit=limit)
        return [
            AlertResponse(
                severity=a.severity.value,
                title=a.title,
                message=a.message,
                timestamp=a.timestamp,
            )
            for a in recent
        ]

    @app.get("/markets")
    async def get_markets():
        return {"markets": market_ids or []}

    return app


async def run_api_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    health_monitor=None,
    portfolio_manager=None,
    analytics=None,
    alert_router=None,
    market_ids: Optional[List[str]] = None,
    start_time: Optional[float] = None,
) -> None:
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI not installed. Run: pip install fastapi uvicorn")
        return

    import uvicorn
    app = create_app(health_monitor, portfolio_manager, analytics, alert_router, market_ids, start_time)
    if app:
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
