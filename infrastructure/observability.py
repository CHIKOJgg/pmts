import asyncio
import logging
from typing import Dict, Any, Callable, List
from aiohttp import web

logger = logging.getLogger(__name__)

class ObservabilityServer:
    """
    Step 6: Observability + Health Monitoring
    Provides a /health endpoint for orchestration checks 
    and a /metrics endpoint for Prometheus/JSON export.
    """
    def __init__(self, port: int = 8080):
        self.port = port
        self.app = web.Application()
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/metrics', self.handle_metrics)
        self.runner: getattr(web, 'AppRunner', None) = None
        self.site: getattr(web, 'TCPSite', None) = None
        self.metrics_providers: List[Callable[[], Dict[str, Any]]] = []

    def register_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        """Register a callback that returns a dictionary of metrics."""
        self.metrics_providers.append(provider)

    async def handle_health(self, request: web.Request) -> web.Response:
        """Simple health check endpoint."""
        return web.json_response({"status": "ok"})

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """Aggregates and returns metrics from all registered providers."""
        metrics: Dict[str, Any] = {}
        for provider in self.metrics_providers:
            try:
                metrics.update(provider())
            except Exception as e:
                logger.error("Error gathering metrics: %s", e)
        return web.json_response(metrics)

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()
        logger.info("Observability server listening on port %d", self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            logger.info("Observability server stopped")
