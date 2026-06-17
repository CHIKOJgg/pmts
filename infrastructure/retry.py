from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def async_retry(
    max_attempts: int = 3,
    base_delay_s: float = 0.5,
    max_delay_s: float = 10.0,
    exponential_base: float = 2.0,
    retryable_exceptions: Optional[tuple[type[Exception], ...]] = None,
) -> Callable[[F], F]:
    if retryable_exceptions is None:
        retryable_exceptions = (
            ConnectionError,
            TimeoutError,
            OSError,
        )

    def _is_retryable(exc: Exception) -> bool:
        return isinstance(exc, retryable_exceptions)

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Optional[Exception] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    if _is_retryable(exc) and attempt < max_attempts:
                        last_exc = exc
                        delay = min(base_delay_s * (exponential_base ** (attempt - 1)), max_delay_s)
                        logger.warning(
                            "%s attempt %d/%d failed: %s. Retrying in %.1fs...",
                            func.__name__, attempt, max_attempts, exc, delay,
                        )
                        await asyncio.sleep(delay)
                    elif attempt >= max_attempts:
                        last_exc = exc
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__name__, max_attempts, exc,
                        )
                    else:
                        raise
            raise last_exc  # type: ignore[misc]
        return wrapper  # type: ignore[return-value]
    return decorator
