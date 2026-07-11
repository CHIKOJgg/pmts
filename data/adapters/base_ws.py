import asyncio
import logging
from typing import Any, List, Optional

import websockets

from data.market_data_provider import _SnapshotCB
from infrastructure.observability import API_ERRORS_TOTAL, RECONNECT_TOTAL
from src.clock import Clock, LiveClock
from src.enums import Platform

logger = logging.getLogger(__name__)

ConnectionClosed = getattr(getattr(websockets, "exceptions", None), "ConnectionClosed", Exception)


class BaseWsAdapter:
    """
    Shared WebSocket feed plumbing for the Polymarket / Opinion venues.

    Subclasses implement venue-specific subscription handshake (``_subscribe``)
    and message parsing (``_handle_message``). This base owns the connection
    lifecycle, exponential-backoff reconnection, and the message dispatch loop.
    """

    PLATFORM: Platform = Platform.POLYMARKET

    def __init__(
        self,
        market_ids: List[str],
        ws_url: str,
        taker_fee_bps: int,
        market_id_map: Optional[dict[str, str]] = None,
        clock: Clock = LiveClock(),
    ) -> None:
        self._market_ids = market_ids
        self._ws_url = ws_url
        self._taker_fee_bps = taker_fee_bps
        self._market_id_map = market_id_map or {}
        self._callback: Optional[_SnapshotCB] = None
        self._clock = clock

        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def platform(self) -> Platform:
        return self.PLATFORM

    def set_snapshot_callback(self, cb: _SnapshotCB) -> None:
        self._callback = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("%s WS adapter started for %d markets", self.PLATFORM.value, len(self._market_ids))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("%s WS adapter stopped", self.PLATFORM.value)

    async def _subscribe(self, ws: Any) -> None:
        """Venue-specific subscribe handshake. Raise on rejection."""
        raise NotImplementedError

    async def _run_loop(self) -> None:
        retry_delay = 1.0
        subscribe_task: Optional[asyncio.Task[None]] = None
        while self._running:
            if subscribe_task and not subscribe_task.done():
                try:
                    subscribe_task.cancel()
                    await subscribe_task
                except asyncio.CancelledError:
                    pass

            RECONNECT_TOTAL.labels(platform=self.PLATFORM.value).inc()
            try:
                async with websockets.connect(self._ws_url) as ws:
                    retry_delay = 1.0  # Reset delay on successful connection

                    await self._subscribe(ws)
                    await self._process_messages(ws)
            except Exception as exc:
                if not self._running:
                    break
                logger.error("%s WS error: %s. Retrying in %.1fs...", self.PLATFORM.value, exc, retry_delay)
                API_ERRORS_TOTAL.labels(platform=self.PLATFORM.value, error_type=type(exc).__name__).inc()

                if subscribe_task and not subscribe_task.done():
                    try:
                        subscribe_task.cancel()
                        await subscribe_task
                    except asyncio.CancelledError:
                        pass
                    subscribe_task = None

                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)

    async def _process_messages(self, ws: Any) -> None:
        try:
            async for message in ws:
                if not self._running:
                    break
                await self._handle_message(message)
        except ConnectionClosed:
            logger.info("%s WS connection closed", self.PLATFORM.value)
        except Exception as exc:
            if self._running:
                logger.error("Error processing %s WS messages: %s", self.PLATFORM.value, exc)
            raise

    async def _handle_message(self, message: Any) -> None:
        raise NotImplementedError
