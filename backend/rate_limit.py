from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimitExceeded(RuntimeError):
    """Raised when a client exceeds the configured request budget."""


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._time_fn = time_fn
        self.history: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_id: str) -> None:
        now = self._time_fn()
        bucket = self.history[client_id]
        boundary = now - self.window_seconds
        while bucket and bucket[0] <= boundary:
            bucket.popleft()
        if len(bucket) >= self.limit:
            raise RateLimitExceeded("rate limit exceeded")
        bucket.append(now)

    def clear(self) -> None:
        self.history.clear()
