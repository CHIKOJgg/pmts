from __future__ import annotations

import inspect
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, cast

if TYPE_CHECKING:
    from execution.engine import ExecutionEngine
    from risk.engine import RiskEngine
    from risk.kill_switch import KillSwitch
    from src.protocols import MarketDataProvider, PortfolioStore

try:
    from aiohttp import web
except Exception:  # pragma: no cover - import guard for backtest/offline environments
    web = None  # type: ignore[assignment]

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except Exception:  # pragma: no cover - import guard for backtest/offline environments
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    class _NoOpMetric:
        def labels(self, *args: Any, **kwargs: Any) -> _NoOpMetric:
            return self

        def set(self, *args: Any, **kwargs: Any) -> None:
            return None

        def inc(self, *args: Any, **kwargs: Any) -> None:
            return None

        def observe(self, *args: Any, **kwargs: Any) -> None:
            return None

    def Gauge(*args: Any, **kwargs: Any) -> _NoOpMetric:  # type: ignore[no-redef]
        return _NoOpMetric()

    def Counter(*args: Any, **kwargs: Any) -> _NoOpMetric:  # type: ignore[no-redef]
        return _NoOpMetric()

    def Histogram(*args: Any, **kwargs: Any) -> _NoOpMetric:  # type: ignore[no-redef]
        return _NoOpMetric()

    def generate_latest(*args: Any, **kwargs: Any) -> bytes:  # type: ignore[misc]
        return b""

logger = logging.getLogger(__name__)

# Prometheus Metrics Definitions
# Gauge for last feed timestamp
FEED_LAST_TS = Gauge("pmts_feed_last_ts_seconds", "Last timestamp received from feed", ["platform", "market_id"])

# Counter for strategy proposals
PROPOSALS_TOTAL = Counter("pmts_proposals_total", "Total order proposals", ["strategy", "verdict"])

# Counters for fills and volume
FILLS_TOTAL = Counter("pmts_fills_total", "Total fills", ["platform", "strategy"])
FILL_USDC_TOTAL = Counter("pmts_fill_usdc_total", "Total filled USDC", ["platform"])
STRATEGY_FILL_USDC_TOTAL = Counter(
    "pmts_strategy_fill_usdc_total",
    "Total filled USDC attributed to strategy flow",
    ["strategy"],
)

# Gauge for exposure per market
OPEN_EXPOSURE_USDC = Gauge("pmts_open_exposure_usdc", "Current open exposure in USDC", ["market_id"])
PORTFOLIO_MTM_USDC = Gauge("pmts_portfolio_mtm_usdc", "Current portfolio MTM in USDC")
PORTFOLIO_REALISED_PNL_USDC = Gauge(
    "pmts_total_realised_pnl_usdc",
    "Total realised PnL in USDC",
)
CAPITAL_UTILIZATION = Gauge(
    "pmts_capital_utilization",
    "Capital utilization ratio (reserved / equity)",
)
ACTIVE_ORDERS_COUNT = Gauge(
    "pmts_active_orders_count",
    "Active open orders count",
    ["platform"],
)

# Risk metrics
DRAWDOWN_PCT = Gauge("pmts_drawdown_pct", "Current portfolio drawdown percentage")
KILL_SWITCH_ACTIVE = Gauge("pmts_kill_switch_active", "Kill switch status (1=active, 0=inactive)")

# Latency histogram (seconds)
ORDER_LATENCY = Histogram("pmts_order_latency_seconds", "Order execution latency", ["platform"])

# Error and reliability counters
API_ERRORS_TOTAL = Counter("pmts_api_errors_total", "Total API errors", ["platform", "error_type"])
RECONNECT_TOTAL = Counter("pmts_reconnect_total", "Total feed reconnections", ["platform"])

class HealthMonitor:
    """
    Central health state tracker for Readiness and Liveness checks (Step 7).
    Exchange connectivity is cached with a short TTL to avoid expensive calls per probe.
    """
    def __init__(
        self,
        mdp: MarketDataProvider,
        engines: List[ExecutionEngine],
        risk: RiskEngine,
        store: PortfolioStore,
        kill_switch: KillSwitch,
        obs_server: Optional[ObservabilityServer] = None,
        liveness_timeout_s: float = 30.0,
        connectivity_ttl_s: float = 10.0,
        mode: str = "live",
    ):
        self.mdp = mdp
        self.engines = engines
        self.risk = risk
        self.store = store
        self.kill_switch = kill_switch
        self.obs_server = obs_server
        self._last_liveness_tick = time.time()
        self._liveness_timeout_s = liveness_timeout_s
        self._connectivity_cache: Dict[str, tuple[float, bool]] = {}
        self._connectivity_ttl_s = connectivity_ttl_s
        self.mode = mode

    def tick_liveness(self) -> None:
        """Update the liveness timestamp to indicate the event loop is running."""
        self._last_liveness_tick = time.time()

    async def _check_engine_connectivity(self, engine: ExecutionEngine) -> bool:
        """Check exchange connectivity with TTL cache to avoid rate-limit issues."""
        client = getattr(engine, "_client", None)
        if client is None:
            return False
        plat = client.platform.value
        now = time.time()
        cached = self._connectivity_cache.get(plat)
        if cached is not None:
            ts, ok = cached
            if now - ts < self._connectivity_ttl_s:
                return ok
        try:
            ok = bool(await client.verify_connectivity())
        except Exception:
            ok = False
        self._connectivity_cache[plat] = (now, ok)
        return ok

    async def check_readiness(self) -> Dict[str, Any]:
        """Strict readiness verification for orchestration."""
        details: Dict[str, Any] = {}
        is_ready = True

        # 1. WS Feeds (at least one platform must have recent data)
        mdp_health = self.mdp.get_health()
        details["ws_feeds"] = mdp_health
        if not any(h["alive"] for h in mdp_health.values()):
            is_ready = False

        # 2. Exchange API & Reconciliation
        details["engines"] = {}
        for engine in self.engines:
            client = getattr(engine, "_client", None)
            if client is None:
                continue
            plat = client.platform.value
            recon = getattr(engine, "reconciliation_complete", False)

            # Use cached connectivity check to avoid expensive calls per probe
            api_ok = await self._check_engine_connectivity(engine)

            details["engines"][plat] = {
                "reconciliation_complete": recon,
                "api_connectivity": api_ok
            }
            if not recon or not api_ok:
                is_ready = False

        # 3. SQLite
        db_ok = self.store.is_healthy()
        details["sqlite"] = {"alive": db_ok}
        if not db_ok:
            is_ready = False

        # 4. Risk & KillSwitch
        ks_active = self.kill_switch.is_active
        risk_recon = getattr(self.risk, "reconciliation_complete", False)

        details["risk"] = {
            "kill_switch_active": ks_active,
            "reconciliation_complete": risk_recon
        }
        if ks_active or not risk_recon:
            is_ready = False

        # 5. Observability Server
        if self.obs_server:
            obs_err = self.obs_server.in_error_state
            details["observability"] = {"error": obs_err}
            if obs_err:
                is_ready = False

        details["mode"] = self.mode
        status = "READY" if is_ready else ("DEGRADED" if self.mode in ("paper", "dry-run") else "NOT_READY")
        return {"status": status, "details": details}

    def check_liveness(self) -> Dict[str, Any]:
        """Liveness check based on event loop tick frequency."""
        age = time.time() - self._last_liveness_tick
        alive = age < self._liveness_timeout_s
        return {
            "status": "ALIVE" if alive else "DEAD",
            "age_s": round(age, 3),
            "timeout_s": self._liveness_timeout_s
        }

class ObservabilityServer:
    """
    Observability + Health Monitoring
    Provides a /health endpoint for orchestration checks,
    a /metrics endpoint for Prometheus export,
    a /metrics/json endpoint for JSON export,
    and /kill-switch/activate and /kill-switch/reset endpoints.
    """
    def __init__(self, port: int = 8080, bind_host: str = "127.0.0.1"):
        if web is None:
            raise RuntimeError("aiohttp.web is required to start ObservabilityServer")
        self.port = port
        self.bind_host = bind_host
        self.app = web.Application()
        self.app.router.add_get('/health', self.handle_liveness)
        self.app.router.add_get('/ready', self.handle_readiness)
        self.app.router.add_get('/metrics', self.handle_metrics_prometheus)
        self.app.router.add_get('/metrics/json', self.handle_metrics_json)
        # Dashboard + data API (served by this same server so the UI just works)
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/dashboard', self.handle_index)
        self.app.router.add_get('/api/summary', self.handle_summary)
        self.app.router.add_get('/api/positions', self.handle_positions)
        self.app.router.add_get('/api/opportunities', self.handle_opportunities)
        self.app.router.add_get('/api/trades', self.handle_trades)
        self.app.router.add_get('/api/performance', self.handle_performance)
        self.app.router.add_get('/api/alerts', self.handle_alerts)
        self.app.router.add_post('/kill-switch/activate', self.handle_kill_switch_activate)
        self.app.router.add_post('/kill-switch/reset', self.handle_kill_switch_reset)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.metrics_providers: List[Callable[[], Dict[str, Any]]] = []
        self.health_monitor: Optional[HealthMonitor] = None
        self.in_error_state: bool = False
        self._kill_switch_token: Optional[str] = None
        self._kill_switch_reset_callback: Optional[Callable[..., Any]] = None
        self._kill_switch_activate_callback: Optional[Callable[..., Any]] = None
        self._reset_attempts: List[float] = []
        self._reset_rate_limit_window_s: float = 60.0
        self._max_resets_per_window: int = 5
        # Dashboard data sources (populated from main.py)
        self._dashboard_path = Path(__file__).parent / "dashboard.html"
        self._portfolio: Any = None
        self._orchestrator: Any = None
        self._alert_router: Any = None
        self._analytics: Any = None

    def register_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        """Register a callback that returns a dictionary of metrics for JSON export."""
        self.metrics_providers.append(provider)

    def set_health_monitor(self, monitor: HealthMonitor) -> None:
        self.health_monitor = monitor

    def set_dashboard_sources(
        self,
        portfolio: Any = None,
        orchestrator: Any = None,
        alert_router: Any = None,
        analytics: Any = None,
    ) -> None:
        """Wire dashboard data sources. Called from main.py after construction."""
        if portfolio is not None:
            self._portfolio = portfolio
        if orchestrator is not None:
            self._orchestrator = orchestrator
        if alert_router is not None:
            self._alert_router = alert_router
        if analytics is not None:
            self._analytics = analytics

    async def handle_liveness(self, request: web.Request) -> web.Response:
        """Liveness check (is the event loop stuck?)."""
        if not self.health_monitor:
            return web.json_response({"status": "error", "message": "HealthMonitor not set"}, status=500)

        health = self.health_monitor.check_liveness()
        status = 200 if health["status"] == "ALIVE" else 503
        return web.json_response(health, status=status)

    async def handle_readiness(self, request: web.Request) -> web.Response:
        """Readiness check (is the system ready to trade?)."""
        if not self.health_monitor:
            return web.json_response({"status": "error", "message": "HealthMonitor not set"}, status=500)

        ready = await self.health_monitor.check_readiness()
        status = 200 if ready["status"] in ("READY", "DEGRADED") else 503
        return web.json_response(ready, status=status)

    async def handle_metrics_prometheus(self, request: web.Request) -> web.Response:
        """Returns metrics in Prometheus format."""
        data = generate_latest()
        return web.Response(body=data, content_type=CONTENT_TYPE_LATEST)

    async def handle_metrics_json(self, request: web.Request) -> web.Response:
        """Aggregates and returns metrics from all registered providers in JSON format."""
        metrics: Dict[str, Any] = {}
        for provider in self.metrics_providers:
            try:
                metrics.update(provider())
            except Exception as e:
                logger.error("Error gathering JSON metrics: %s", e)
        return web.json_response(metrics)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve the single-page dashboard HTML."""
        try:
            body = self._dashboard_path.read_text(encoding="utf-8")
        except OSError:
            return web.Response(text="dashboard.html not found", status=404)
        return web.Response(text=body, content_type="text/html")

    def _portfolio_payload(self) -> Dict[str, Any]:
        if self._portfolio is None:
            return {}
        pm = self._portfolio
        snap = pm.build_snapshot()
        initial = pm.initial_capital
        equity = snap["total_mtm_usdc"]
        return {
            "total_value": equity,
            "cash_usdc": snap["total_cash_usdc"],
            "reserved_capital": getattr(pm, "reserved_capital", 0.0),
            "initial_capital": initial,
            "peak_equity": snap["peak_equity_usdc"],
            "realised_pnl": snap["total_realised_pnl"],
            "total_return_pct": ((equity - initial) / initial * 100.0) if initial > 0 else 0.0,
            "drawdown_pct": snap["mtm_drawdown_pct"] * 100.0,
            "positions_count": len(snap.get("positions", [])),
        }

    def _positions_payload(self) -> List[Dict[str, Any]]:
        if self._portfolio is None:
            return []
        # Cost basis per (market, platform)
        cost_map: Dict[Any, float] = {}
        for p in self._portfolio.get_all_positions():
            yes_cost = (p.yes_holdings_pm * (p.avg_cost_yes_pm or 0.0)) + (p.yes_holdings_op * (p.avg_cost_yes_op or 0.0))
            no_cost = (p.no_holdings_pm * (p.avg_cost_no_pm or 0.0)) + (p.no_holdings_op * (p.avg_cost_no_op or 0.0))
            key = (p.market_id, "polymarket" if (p.yes_holdings_pm or p.no_holdings_pm) else "opinion")
            cost_map[key] = yes_cost + no_cost
        out = []
        snap = self._portfolio.build_snapshot()
        for pos in snap.get("positions", []):
            mtm = pos.get("mtm_usdc", 0.0)
            cost = cost_map.get((pos["market_id"], pos["platform"]), 0.0)
            out.append({
                "market_id": pos["market_id"],
                "platform": pos["platform"],
                "yes_qty": pos["yes_qty"],
                "no_qty": pos["no_qty"],
                "net_delta": pos.get("net_delta", 0.0),
                "mtm_usdc": mtm,
                "cost_basis_usdc": cost,
                "unrealized_pnl": mtm - cost,
                "realised_pnl": pos.get("realised_pnl", 0.0),
            })
        return out

    def _risk_payload(self) -> Dict[str, Any]:
        risk = getattr(self.health_monitor, "risk", None)
        if risk is None:
            return {}
        return {
            "kill_switch_active": bool(getattr(risk, "kill_switch_active", False)),
            "drawdown_pct": getattr(risk, "current_drawdown", 0.0) * 100.0,
            "reserved_capital": getattr(risk, "reserved_capital", 0.0),
            "total_evaluated": getattr(risk, "total_evaluated", 0),
            "total_approved": getattr(risk, "total_approved", 0),
            "total_rejected": getattr(risk, "total_rejected", 0),
            "rejections_by_reason": dict(getattr(risk, "rejections_by_reason", {})),
        }

    def _orchestrator_payload(self) -> Dict[str, Any]:
        orch = self._orchestrator
        if orch is None:
            return {}
        return {
            "proposals_evaluated": getattr(orch, "proposals_evaluated", 0),
            "proposals_approved": getattr(orch, "proposals_approved", 0),
            "proposals_rejected": getattr(orch, "proposals_rejected", 0),
        }

    def _performance_payload(self) -> Dict[str, Any]:
        if self._analytics is None or self._portfolio is None:
            return {}
        try:
            m = self._analytics.compute_metrics(self._portfolio.initial_capital)
        except Exception:
            return {}
        return {
            "total_return_pct": m.total_return_pct,
            "sharpe_ratio": m.sharpe_ratio,
            "sortino_ratio": m.sortino_ratio,
            "max_drawdown_pct": m.max_drawdown_pct,
            "win_rate": m.win_rate,
            "profit_factor": m.profit_factor,
            "avg_win": m.avg_win,
            "avg_loss": m.avg_loss,
            "total_trades": m.total_trades,
            "avg_hold_time_ms": m.avg_hold_time_ms,
        }

    def _alerts_payload(self, limit: int = 50) -> List[Dict[str, Any]]:
        if self._alert_router is None:
            return []
        try:
            recent = self._alert_router.get_recent(limit=limit)
        except Exception:
            return []
        return [
            {
                "severity": getattr(a.severity, "value", str(a.severity)),
                "title": a.title,
                "message": a.message,
                "timestamp": a.timestamp,
            }
            for a in recent
        ]

    async def handle_summary(self, request: web.Request) -> web.Response:
        """Single combined payload the dashboard polls — avoids many round trips."""
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        summary = {
            "status": self.health_monitor.check_liveness()["status"] if self.health_monitor else "UNKNOWN",
            "portfolio": self._portfolio_payload(),
            "risk": self._risk_payload(),
            "orchestrator": self._orchestrator_payload(),
            "performance": self._performance_payload(),
            "positions": self._positions_payload(),
            "opportunities": self._orchestrator.get_recent_opportunities(limit) if self._orchestrator else [],
            "trades": self._orchestrator.get_recent_trades(50) if self._orchestrator else [],
            "alerts": self._alerts_payload(50),
            "market_data": self._metrics_provider_market_data(),
        }
        return web.json_response(summary)

    def _metrics_provider_market_data(self) -> Dict[str, Any]:
        for provider in self.metrics_providers:
            try:
                data = provider()
                md = data.get("market_data")
                if md:
                    return cast(Dict[str, Any], md)
            except Exception:
                continue
        return {}

    async def handle_positions(self, request: web.Request) -> web.Response:
        return web.json_response(self._positions_payload())

    async def handle_opportunities(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        items = self._orchestrator.get_recent_opportunities(limit) if self._orchestrator else []
        return web.json_response(items)

    async def handle_trades(self, request: web.Request) -> web.Response:
        items = self._orchestrator.get_recent_trades(50) if self._orchestrator else []
        return web.json_response(items)

    async def handle_performance(self, request: web.Request) -> web.Response:
        return web.json_response(self._performance_payload())

    async def handle_alerts(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        return web.json_response(self._alerts_payload(limit))

    def set_kill_switch_config(
        self,
        token: str,
        reset_callback: Optional[Callable[..., Any]] = None,
        activate_callback: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Configure kill-switch endpoint authentication and reset callback."""
        self._kill_switch_token = token
        self._kill_switch_reset_callback = reset_callback
        self._kill_switch_activate_callback = activate_callback

    async def handle_kill_switch_activate(self, request: web.Request) -> web.Response:
        """POST /kill-switch/activate — activate the kill switch (requires token)."""
        try:
            body = await request.json()
        except Exception:
            logger.debug("Failed to parse request JSON; using empty body")
            body = {}

        token = body.get("token", "")
        reason = body.get("reason", "operator_http")
        operator_id = body.get("operator_id", "unknown")

        if not self._kill_switch_token or token != self._kill_switch_token:
            logger.warning("Kill switch activate rejected — bad token (operator=%s)", operator_id)
            return web.json_response({"error": "invalid token"}, status=403)

        if not self.health_monitor:
            return web.json_response({"error": "health monitor not available"}, status=503)

        source_ip = request.remote or "unknown"
        if self._kill_switch_activate_callback:
            result = self._kill_switch_activate_callback(reason)
            if inspect.isawaitable(result):
                await result
        else:
            self.health_monitor.risk.manual_activate(reason)
        logger.critical(
            "KILL SWITCH ACTIVATED via HTTP by operator=%s source=%s reason=%s",
            operator_id,
            source_ip,
            reason,
        )
        return web.json_response({"status": "activated", "reason": reason})

    async def handle_kill_switch_reset(self, request: web.Request) -> web.Response:
        """POST /kill-switch/reset — reset the kill switch (requires token + rate limit)."""
        try:
            body = await request.json()
        except Exception:
            logger.debug("Failed to parse request JSON; using empty body")
            body = {}

        token = body.get("token", "")
        operator_id = body.get("operator_id", "unknown")

        if not self._kill_switch_token or token != self._kill_switch_token:
            logger.warning("Kill switch reset rejected — bad token (operator=%s)", operator_id)
            return web.json_response({"error": "invalid token"}, status=403)

        now = time.time()
        self._reset_attempts = [
            t for t in self._reset_attempts if now - t < self._reset_rate_limit_window_s
        ]
        if len(self._reset_attempts) >= self._max_resets_per_window:
            logger.warning(
                "Kill switch reset rate-limited — %d attempts in %ds (operator=%s)",
                len(self._reset_attempts),
                self._reset_rate_limit_window_s,
                operator_id,
            )
            return web.json_response({"error": "rate limited"}, status=429)

        self._reset_attempts.append(now)

        if not self.health_monitor:
            return web.json_response({"error": "health monitor not available"}, status=503)

        source_ip = request.remote or "unknown"
        success = self.health_monitor.risk.reset_kill_switch(token, operator_id)
        if success:
            logger.warning(
                "KILL SWITCH RESET via HTTP by operator=%s source=%s",
                operator_id,
                source_ip,
            )
            if self._kill_switch_reset_callback:
                try:
                    self._kill_switch_reset_callback()
                except Exception as exc:
                    logger.error("Kill switch reset callback failed: %s", exc)
            return web.json_response({"status": "reset"})
        else:
            return web.json_response({"error": "reset failed"}, status=400)

    async def start(self) -> None:
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, self.bind_host, self.port)
            await self.site.start()
            self.in_error_state = False
            logger.info("Observability server listening on port %d", self.port)
        except Exception as e:
            self.in_error_state = True
            logger.error("Failed to start observability server: %s", e)
            raise

    async def stop(self) -> None:
        try:
            if self.runner:
                await self.runner.cleanup()
                logger.info("Observability server stopped")
        except Exception as e:
            self.in_error_state = True
            logger.error("Error stopping observability server: %s", e)
