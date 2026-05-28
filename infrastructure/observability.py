from __future__ import annotations

import logging
import time
from typing import Dict, Any, Callable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.protocols import MarketDataProvider, PortfolioStore
    from execution.engine import ExecutionEngine
    from risk.engine import RiskEngine
    from risk.kill_switch import KillSwitch

try:
    from aiohttp import web
except Exception:  # pragma: no cover - import guard for backtest/offline environments
    web = None

try:
    from prometheus_client import Gauge, Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest
except Exception:  # pragma: no cover - import guard for backtest/offline environments
    CONTENT_TYPE_LATEST = "text/plain; charset=utf-8"

    class _NoOpMetric:
        def labels(self, *args, **kwargs):
            return self

        def set(self, *args, **kwargs):
            return None

        def inc(self, *args, **kwargs):
            return None

        def observe(self, *args, **kwargs):
            return None

    def Gauge(*args, **kwargs):  # type: ignore[misc]
        return _NoOpMetric()

    def Counter(*args, **kwargs):  # type: ignore[misc]
        return _NoOpMetric()

    def Histogram(*args, **kwargs):  # type: ignore[misc]
        return _NoOpMetric()

    def generate_latest(*args, **kwargs):  # type: ignore[misc]
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
    ):
        self.mdp = mdp
        self.engines = engines
        self.risk = risk
        self.store = store
        self.kill_switch = kill_switch
        self.obs_server = obs_server
        self._last_liveness_tick = time.time()
        self._liveness_timeout_s = liveness_timeout_s
        self._connectivity_cache: Dict[str, Tuple[float, bool]] = {}
        self._connectivity_ttl_s = connectivity_ttl_s

    def tick_liveness(self) -> None:
        """Update the liveness timestamp to indicate the event loop is running."""
        self._last_liveness_tick = time.time()

    async def _check_engine_connectivity(self, engine: ExecutionEngine) -> bool:
        """Check exchange connectivity with TTL cache to avoid rate-limit issues."""
        plat = engine._client.platform.value
        now = time.time()
        cached = self._connectivity_cache.get(plat)
        if cached is not None:
            ts, ok = cached
            if now - ts < self._connectivity_ttl_s:
                return ok
        try:
            ok = await engine._client.verify_connectivity()
        except Exception:
            ok = False
        self._connectivity_cache[plat] = (now, ok)
        return ok

    async def check_readiness(self) -> Dict[str, Any]:
        """Strict readiness verification for orchestration."""
        details = {}
        is_ready = True

        # 1. WS Feeds (at least one platform must have recent data)
        mdp_health = self.mdp.get_health()
        details["ws_feeds"] = mdp_health
        if not any(h["alive"] for h in mdp_health.values()):
            is_ready = False

        # 2. Exchange API & Reconciliation
        details["engines"] = {}
        for engine in self.engines:
            plat = engine._client.platform.value
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

        return {"status": "READY" if is_ready else "NOT_READY", "details": details}

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
        self.app.router.add_post('/kill-switch/activate', self.handle_kill_switch_activate)
        self.app.router.add_post('/kill-switch/reset', self.handle_kill_switch_reset)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.metrics_providers: List[Callable[[], Dict[str, Any]]] = []
        self.health_monitor: Optional[HealthMonitor] = None
        self.in_error_state: bool = False
        self._kill_switch_token: Optional[str] = None
        self._kill_switch_reset_callback: Optional[Callable] = None
        self._reset_attempts: List[float] = []
        self._reset_rate_limit_window_s: float = 60.0
        self._max_resets_per_window: int = 5

    def register_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        """Register a callback that returns a dictionary of metrics for JSON export."""
        self.metrics_providers.append(provider)

    def set_health_monitor(self, monitor: HealthMonitor) -> None:
        self.health_monitor = monitor

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
        status = 200 if ready["status"] == "READY" else 503
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

    def set_kill_switch_config(
        self,
        token: str,
        reset_callback: Optional[Callable] = None,
    ) -> None:
        """Configure kill-switch endpoint authentication and reset callback."""
        self._kill_switch_token = token
        self._kill_switch_reset_callback = reset_callback

    async def handle_kill_switch_activate(self, request: web.Request) -> web.Response:
        """POST /kill-switch/activate — activate the kill switch (requires token)."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        token = body.get("token", "")
        reason = body.get("reason", "operator_http")
        operator_id = body.get("operator_id", "unknown")

        if not self._kill_switch_token or token != self._kill_switch_token:
            logger.warning("Kill switch activate rejected — bad token (operator=%s)", operator_id)
            return web.json_response({"error": "invalid token"}, status=403)

        if not self.health_monitor:
            return web.json_response({"error": "health monitor not available"}, status=503)

        self.health_monitor.risk.manual_activate(reason)
        source_ip = request.remote or "unknown"
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
