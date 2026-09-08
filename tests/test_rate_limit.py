from __future__ import annotations

import pytest

from backend.rate_limit import RateLimitExceeded, SlidingWindowRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_rate_limit_is_scoped_per_client() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(
        limit=2,
        window_seconds=10,
        time_fn=clock,
    )

    limiter.check("client-a")
    limiter.check("client-a")
    limiter.check("client-b")

    with pytest.raises(RateLimitExceeded, match="rate limit exceeded"):
        limiter.check("client-a")


def test_rate_limit_releases_requests_after_window() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(
        limit=1,
        window_seconds=10,
        time_fn=clock,
    )

    limiter.check("client-a")
    with pytest.raises(RateLimitExceeded):
        limiter.check("client-a")

    clock.advance(10)
    limiter.check("client-a")
    assert list(limiter.history["client-a"]) == [10.0]
