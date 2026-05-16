"""api/server.py — FastAPI server for web dashboard and external integrations."""
from __future__ import annotations

import logging
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
        return HealthResponse(
            status="healthy" if readiness.get("ready") else "unhealthy",
            uptime_seconds=0.0,
            active_strategies=0,
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
                platform=p.platform.value,
                yes_qty=p.yes_qty,
                no_qty=p.no_qty,
                avg_cost_yes=p.avg_cost_yes,
                avg_cost_no=p.avg_cost_no,
                unrealized_pnl=p.realised_pnl,
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
        return []

    @app.get("/markets")
    async def get_markets():
        return {"markets": []}

    return app


async def run_api_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    health_monitor=None,
    portfolio_manager=None,
    analytics=None,
    alert_router=None,
) -> None:
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI not installed. Run: pip install fastapi uvicorn")
        return

    import uvicorn
    app = create_app(health_monitor, portfolio_manager, analytics, alert_router)
    if app:
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
