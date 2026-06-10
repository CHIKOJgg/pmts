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
    avg_cost_yes: Optional[float] = None
    avg_cost_no: Optional[float] = None
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


class KillSwitchResponse(BaseModel):
    success: bool
    message: str


class ReloadResponse(BaseModel):
    success: bool
    message: str


class CancelOrderRequest(BaseModel):
    proposal_id: str


def create_app(
    health_monitor=None,
    portfolio_manager=None,
    analytics=None,
    alert_router=None,
    market_ids: Optional[List[str]] = None,
    start_time: Optional[float] = None,
    kill_switch=None,
    kill_switch_token: str = "",
    orchestrator=None,
    risk_engine=None,
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
        active_ks = False
        if kill_switch is not None:
            active_ks = kill_switch.is_active
        elif risk_engine is not None:
            active_ks = risk_engine.kill_switch_active
        return HealthResponse(
            status="healthy" if readiness.get("ready") else "unhealthy",
            uptime_seconds=uptime,
            active_strategies=readiness.get("active_strategies", 0),
            kill_switch=active_ks,
        )

    @app.get("/positions", response_model=List[PositionResponse])
    async def get_positions(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        if portfolio_manager is None:
            raise HTTPException(status_code=503, detail="Portfolio manager not available")
        positions = portfolio_manager.get_all_positions()
        page = positions[offset:offset + limit]
        return [
            PositionResponse(
                market_id=p.market_id,
                platform="polymarket" if p.yes_holdings_pm > 0 or p.no_holdings_pm > 0 else "opinion",
                yes_qty=p.yes_holdings_pm + p.yes_holdings_op,
                no_qty=p.no_holdings_pm + p.no_holdings_op,
                avg_cost_yes=p.avg_cost_yes_pm if p.avg_cost_yes_pm is not None else p.avg_cost_yes_op,
                avg_cost_no=p.avg_cost_no_pm if p.avg_cost_no_pm is not None else p.avg_cost_no_op,
                unrealized_pnl=0.0,
            )
            for p in page
        ]

    @app.get("/metrics", response_model=MetricsResponse)
    async def get_metrics():
        if analytics is None:
            raise HTTPException(status_code=503, detail="Analytics not available")
        initial_cap = portfolio_manager.initial_capital if portfolio_manager else 0
        metrics = analytics.compute_metrics(initial_cap)
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
    async def get_markets(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        all_ids = market_ids or []
        page = all_ids[offset:offset + limit]
        return {"markets": page, "total": len(all_ids), "offset": offset, "limit": limit}

    @app.post("/kill-switch/activate", response_model=KillSwitchResponse)
    async def activate_kill_switch():
        if kill_switch is None:
            raise HTTPException(status_code=503, detail="Kill switch not available")
        kill_switch.activate(
            reason="manual_api",
            mtm_drawdown=0.0,
        )
        if orchestrator is not None:
            await orchestrator.emergency_stop()
        logger.warning("Kill switch activated via API")
        return KillSwitchResponse(success=True, message="Kill switch activated")

    @app.post("/kill-switch/reset", response_model=KillSwitchResponse)
    async def reset_kill_switch(token: str = Query(..., description="Kill switch reset token")):
        if kill_switch is None:
            raise HTTPException(status_code=503, detail="Kill switch not available")
        if not kill_switch_token:
            raise HTTPException(status_code=400, detail="Kill switch token not configured")
        if not kill_switch.reset(token=token):
            raise HTTPException(status_code=403, detail="Invalid kill switch token")
        if orchestrator is not None:
            orchestrator._on_kill_switch_reset()
        logger.warning("Kill switch reset via API")
        return KillSwitchResponse(success=True, message="Kill switch reset")

    @app.post("/cancel-order", response_model=KillSwitchResponse)
    async def cancel_order(req: CancelOrderRequest):
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="Orchestrator not available")
        await orchestrator.cancel_proposal(req.proposal_id)
        return KillSwitchResponse(success=True, message=f"Cancel requested for {req.proposal_id[:8]}")

    @app.post("/reload/config", response_model=ReloadResponse)
    async def reload_config():
        if risk_engine is None:
            raise HTTPException(status_code=503, detail="Risk engine not available")
        risk_engine.reload_limits()
        return ReloadResponse(success=True, message="Risk limits reloaded from store")

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
    kill_switch=None,
    kill_switch_token: str = "",
    orchestrator=None,
    risk_engine=None,
) -> None:
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI not installed. Run: pip install fastapi uvicorn")
        return

    import uvicorn
    app = create_app(
        health_monitor, portfolio_manager, analytics, alert_router,
        market_ids, start_time, kill_switch, kill_switch_token,
        orchestrator, risk_engine,
    )
    if app:
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
