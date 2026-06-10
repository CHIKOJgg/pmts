"""infrastructure/market_watcher.py — Hot-reload for market_registry.json."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_RELOAD_CB = Callable[[dict[str, Any]], None]


class MarketRegistryWatcher:
    """
    Watches market_registry.json for changes and triggers reload callbacks.

    Uses polling (file mtime comparison) since watchdog is not a dependency.
    Poll interval is configurable; defaults to 30 seconds.
    """

    def __init__(
        self,
        file_path: str,
        callback: Optional[_RELOAD_CB] = None,
        poll_interval_s: float = 30.0,
    ) -> None:
        self._file_path = file_path
        self._callback = callback
        self._poll_interval_s = poll_interval_s
        self._last_mtime: float = 0.0
        self._task: Optional[asyncio.Task[None]] = None
        self._stopped: bool = False

    async def start(self) -> None:
        self._stopped = False
        self._last_mtime = self._get_mtime()
        self._task = asyncio.create_task(self._poll_loop(), name="market-registry-watcher")

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _get_mtime(self) -> float:
        try:
            return os.path.getmtime(self._file_path)
        except OSError:
            return 0.0

    def _load_registry(self) -> Optional[dict[str, Any]]:
        try:
            with open(self._file_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("Failed to load market registry: %s", exc)
            return None

    async def _poll_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                return

            mtime = self._get_mtime()
            if mtime > self._last_mtime:
                logger.info("Market registry changed (mtime: %.3f → %.3f), reloading...", self._last_mtime, mtime)
                registry = self._load_registry()
                if registry is not None:
                    self._last_mtime = mtime
                    if self._callback:
                        try:
                            self._callback(registry)
                        except Exception as exc:
                            logger.error("Market registry reload callback failed: %s", exc)
