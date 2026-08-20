"""infrastructure/circuit_breaker.py — Circuit breaker for external API calls."""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit breaker is open."""


class CircuitBreaker:
    """
    Prevents cascading failures by breaking the circuit when errors exceed threshold.

    States:
      CLOSED   — normal operation, calls pass through
      OPEN     — calls are rejected immediately, timer starts
      HALF_OPEN — single test call allowed; success → CLOSED, failure → OPEN again
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._half_open_max_calls = half_open_max_calls

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_ts: float = 0.0
        self._half_open_calls: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._success_count: int = 0
        self._rejected_count: int = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_available(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.HALF_OPEN:
            return self._half_open_calls < self._half_open_max_calls
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_ts >= self._recovery_timeout_s:
                return True
            return False
        return True

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            available = self.is_available
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_ts >= self._recovery_timeout_s:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("Circuit %s → HALF_OPEN (recovery timeout elapsed)", self._name)
                    available = True

            if not available:
                self._rejected_count += 1
                raise CircuitBreakerOpen(
                    f"Circuit breaker '{self._name}' is {self._state.value}, call rejected"
                )

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1

        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            async with self._lock:
                self._failure_count += 1
                self._last_failure_ts = time.monotonic()
                if self._failure_count >= self._failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "Circuit %s → OPEN after %d failures: %s",
                        self._name,
                        self._failure_count,
                        exc,
                    )
                elif self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "Circuit %s → OPEN (half-open test call failed): %s",
                        self._name,
                        exc,
                    )
            raise

        async with self._lock:
            self._success_count += 1
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info("Circuit %s → CLOSED (half-open test call succeeded)", self._name)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

        return result

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0
        self._rejected_count = 0
        logger.info("Circuit %s manually reset to CLOSED", self._name)


class CircuitBreakerExchangeWrapper:
    """
    Wraps an ExchangeClient with per-method circuit breakers.

    Each method (place_order, cancel_order, get_order_status, verify_connectivity,
    get_open_orders) gets its own circuit breaker so one failing method doesn't
    block others.
    """

    def __init__(self, client: Any, base_name: Optional[str] = None) -> None:
        self._client = client
        platform = getattr(client, "platform", "unknown")
        name = base_name or str(getattr(platform, "value", platform))
        self._breakers: dict[str, CircuitBreaker] = {
            "place_order": CircuitBreaker(f"{name}.place_order", failure_threshold=3, recovery_timeout_s=15.0),
            "cancel_order": CircuitBreaker(f"{name}.cancel_order", failure_threshold=5, recovery_timeout_s=10.0),
            "get_order_status": CircuitBreaker(f"{name}.get_order_status", failure_threshold=5, recovery_timeout_s=10.0),
            "verify_connectivity": CircuitBreaker(f"{name}.verify_connectivity", failure_threshold=2, recovery_timeout_s=60.0),
            "get_open_orders": CircuitBreaker(f"{name}.get_open_orders", failure_threshold=3, recovery_timeout_s=30.0),
            "get_market": CircuitBreaker(f"{name}.get_market", failure_threshold=3, recovery_timeout_s=30.0),
            "redeem_market": CircuitBreaker(f"{name}.redeem_market", failure_threshold=3, recovery_timeout_s=60.0),
        }

    @property
    def platform(self) -> Any:
        return self._client.platform

    @property
    def wrapped(self) -> Any:
        return self._client

    async def place_order(self, *args: Any, **kwargs: Any) -> Any:
        cb = self._breakers["place_order"]
        return await cb.call(self._client.place_order, *args, **kwargs)

    async def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        cb = self._breakers["cancel_order"]
        return await cb.call(self._client.cancel_order, *args, **kwargs)

    async def get_order_status(self, *args: Any, **kwargs: Any) -> Any:
        cb = self._breakers["get_order_status"]
        return await cb.call(self._client.get_order_status, *args, **kwargs)

    async def verify_connectivity(self) -> bool:
        cb = self._breakers["verify_connectivity"]
        return bool(await cb.call(self._client.verify_connectivity))

    async def get_open_orders(self, *args: Any, **kwargs: Any) -> Any:
        cb = self._breakers["get_open_orders"]
        return await cb.call(self._client.get_open_orders, *args, **kwargs)

    async def get_market(self, *args: Any, **kwargs: Any) -> Any:
        cb = self._breakers["get_market"]
        return await cb.call(self._client.get_market, *args, **kwargs)

    async def redeem_market(self, *args: Any, **kwargs: Any) -> Any:
        cb = self._breakers["redeem_market"]
        return await cb.call(self._client.redeem_market, *args, **kwargs)

    async def close(self) -> None:
        close_fn = getattr(self._client, "close", None)
        if close_fn is not None:
            await close_fn()

    def get_breaker_states(self) -> dict[str, str]:
        return {name: cb.state.value for name, cb in self._breakers.items()}

    def get_breaker_metrics(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "state": cb.state.value,
                "failures": cb._failure_count,
                "rejected": cb._rejected_count,
                "successes": cb._success_count,
            }
            for name, cb in self._breakers.items()
        }
