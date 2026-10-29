"""Retries with exponential backoff and jitter, for LLM calls and integration API calls."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class TransientError(RuntimeError):
    """A failure worth retrying: rate limits, 5xx responses, timeouts, dropped connections."""


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    is_retryable: Callable[[Exception], bool] = lambda e: isinstance(e, TransientError),
    on_retry: Callable[[int, Exception, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call fn(); on a retryable error wait base_delay * 2^n (+ jitter) and try again, up to `attempts` times."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts or not is_retryable(e):
                raise
            delay = min(base_delay * 2 ** (attempt - 1), max_delay) * (1 + random.uniform(0, 0.25))
            if on_retry:
                on_retry(attempt, e, delay)
            sleep(delay)
    raise AssertionError("unreachable")
