import asyncio
import logging
import time
from typing import Dict, Any, Callable, List, Optional
from aiohttp import web
from prometheus_client import Gauge, Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger(__name__)

# Prometheus Metrics Definitions
# Gauge for last feed timestamp
FEED_LAST_TS = Gauge("pmts_feed_last_ts_seconds", "Last timestamp received from feed", ["platform", "market_id"])

# Counter for strategy proposals
PROPOSALS_TOTAL = Counter("pmts_proposals_total", "Total order proposals", ["strategy", "verdict"])

# Counters for fills and volume
FILLS_TOTAL = Counter("pmts_fills_total", "Total fills", ["platform", "strategy"])
FILL_USDC_TOTAL = Counter("pmts_fill_usdc_total", "Total filled USDC", ["platform"])

# Gauge for exposure per market
OPEN_EXPOSURE_USDC = Gauge("pmts_open_exposure_usdc", "Current open exposure in USDC", ["market_id"])

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
    """
    def __init__(
        self, 
        mdp: Any, 
        engines: List[Any], 
        risk: Any, 
        store: Any, 
        kill_switch: Any,
        obs_server: Optional[Any] = None,
        liveness_timeout_s: float = 30.0
    ):
        self.mdp = mdp
        self.engines = engines
        self.risk = risk
        self.store = store
        self.kill_switch = kill_switch
        self.obs_server = obs_server
        self._last_liveness_tick = time.time()
        self._liveness_timeout_s = liveness_timeout_s

    def tick_liveness(self) -> None:
        """Update the liveness timestamp to indicate the event loop is running."""
        self._last_liveness_tick = time.time()

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
            
            # Perform a fresh connectivity check
            api_ok = False
            try:
                api_ok = await engine._client.verify_connectivity()
            except Exception:
                pass
            
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
    and a /metrics/json endpoint for JSON export.
    """
    def __init__(self, port: int = 8080):
        self.port = port
        self.app = web.Application()
        self.app.router.add_get('/health', self.handle_liveness)
        self.app.router.add_get('/ready', self.handle_readiness)
        self.app.router.add_get('/metrics', self.handle_metrics_prometheus)
        self.app.router.add_get('/metrics/json', self.handle_metrics_json)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.metrics_providers: List[Callable[[], Dict[str, Any]]] = []
        self.health_monitor: Optional[HealthMonitor] = None
        self.in_error_state: bool = False

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

    async def start(self) -> None:
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
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
