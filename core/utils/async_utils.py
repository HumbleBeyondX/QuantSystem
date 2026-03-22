"""Async utilities: retry decorators, rate limiters, timeout helpers."""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, Optional, Type, TypeVar

from core.logging import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential: bool = True,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator for async functions with exponential backoff retry.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay between retries (seconds).
        max_delay: Maximum delay between retries (seconds).
        exponential: Use exponential backoff if True, fixed delay if False.
        exceptions: Tuple of exception types to catch and retry.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        logger.error(
                            "retry_exhausted",
                            func=func.__name__,
                            attempts=max_retries + 1,
                            error=str(e),
                        )
                        raise
                    delay = base_delay * (2**attempt if exponential else 1)
                    delay = min(delay, max_delay)
                    logger.warning(
                        "retry_attempt",
                        func=func.__name__,
                        attempt=attempt + 1,
                        delay=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
            raise last_exception  # type: ignore

        return wrapper  # type: ignore

    return decorator


class AsyncRateLimiter:
    """Token bucket rate limiter for async operations.

    Usage:
        limiter = AsyncRateLimiter(max_calls=10, period=1.0)
        async with limiter:
            await make_api_call()
    """

    def __init__(self, max_calls: int, period: float = 1.0) -> None:
        self._max_calls = max_calls
        self._period = period
        self._semaphore = asyncio.Semaphore(max_calls)
        self._calls: list[float] = []

    async def __aenter__(self) -> "AsyncRateLimiter":
        await self._semaphore.acquire()
        now = time.monotonic()

        # Clean old entries
        self._calls = [t for t in self._calls if now - t < self._period]

        if len(self._calls) >= self._max_calls:
            sleep_time = self._period - (now - self._calls[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        self._calls.append(time.monotonic())
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._semaphore.release()


async def run_with_timeout(
    coro: Any,
    timeout_seconds: float,
    default: Any = None,
) -> Any:
    """Run a coroutine with a timeout, returning default on timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("operation_timeout", timeout=timeout_seconds)
        return default
