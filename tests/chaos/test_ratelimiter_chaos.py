"""Chaos: RateLimiter under real Redis death.

Docs claim: a token-bucket `RateLimiter` with `fail_open=True` returns
an allowed result when the backend raises, and with `fail_open=False`
(the default) the backend error propagates loudly.

We prove both against a real Redis container paused mid-traffic, not a
mocked failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter import RateLimiter
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter

from .conftest import build_client, paused, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from testcontainers.redis import RedisContainer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(60),
]


@pytest.fixture
async def backend(
    redis_container: RedisContainer,
) -> AsyncGenerator[RedisRateLimiterAdapter]:
    """Yield a Redis rate-limiter adapter on a bounded-timeout client."""
    client = build_client(redis_container)
    provider = RedisProvider.from_client(client, own=True)
    async with provider, RedisRateLimiterAdapter(provider=provider) as adapter:
        yield adapter


async def test_fail_open_serves_allowed_while_redis_is_down(
    redis_container: RedisContainer,
    backend: RedisRateLimiterAdapter,
) -> None:
    """`fail_open=True`: paused Redis yields allowed results, not errors.

    Traffic flows, Redis is paused, every acquire returns an allowed
    fallback result while down, then real limiting resumes on unpause.
    """
    limiter = RateLimiter.token_bucket(
        f"chaos-open-{uuid4().hex}",
        capacity=5,
        refill_rate=1,
        fail_open=True,
        backend=backend,
    )

    # Healthy traffic: the bucket limits as normal.
    healthy = [(await limiter.acquire(key="u1")).allowed for _ in range(7)]
    assert healthy[:5] == [True] * 5
    assert healthy[5:] == [False, False], "bucket must throttle past capacity"

    # Fault: freeze Redis. Every call must come back allowed (fail open),
    # never raise, even though the backend is unreachable.
    with paused(redis_container):
        results = [await limiter.acquire(key="u1") for _ in range(5)]
        assert all(r.allowed for r in results), (
            "fail_open=True must serve allowed results while the backend is down"
        )
        # The fallback reports full quota, the documented degraded shape.
        assert all(r.remaining == r.limit for r in results)

    # Recovery: the same client reconnects and real limiting resumes.
    async def limiting_resumed() -> bool:
        key = f"recover-{uuid4().hex}"
        decisions = [(await limiter.acquire(key=key)).allowed for _ in range(7)]
        return decisions[:5] == [True] * 5 and decisions[5] is False

    assert await wait_until(limiting_resumed, timeout=15), (
        "real limiting must resume once Redis is back"
    )


async def test_fail_closed_propagates_backend_error(
    redis_container: RedisContainer,
    backend: RedisRateLimiterAdapter,
) -> None:
    """`fail_open=False` (default): a paused Redis propagates the error.

    The documented loud failure: the backend error is raised, not
    swallowed into a fake allow/deny.
    """
    limiter = RateLimiter.token_bucket(
        f"chaos-closed-{uuid4().hex}",
        capacity=5,
        refill_rate=1,
        fail_open=False,
        backend=backend,
    )

    assert (await limiter.acquire(key="u1")).allowed is True

    with paused(redis_container):
        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await limiter.acquire(key="u1")
        # A real backend/timeout error, not a RateLimit decision.
        assert not isinstance(exc_info.value, RateLimitExceededError), (
            "fail-closed must surface the backend error, not a limit decision"
        )

    async def healthy_again() -> bool:
        return (await limiter.acquire(key=f"r-{uuid4().hex}")).allowed

    assert await wait_until(healthy_again, timeout=15)
