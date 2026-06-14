"""Chaos: CircuitBreaker around a dying dependency.

Docs claim: a circuit breaker trips when a backend dependency is in
pain, after which fast `CircuitBreakerError` admission rejections
replace slow connection errors, and once the dependency recovers a
half-open probe closes the breaker.

The breaker state lives in a memory backend (in-process), while the
protected calls reach into a real Redis container that we pause to
simulate the dependency dying. So the fault is real, the breaker is
the thing under test.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

import anyio
import pytest

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerState,
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.errors import CircuitBreakerError

from .conftest import build_client, paused

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from testcontainers.redis import RedisContainer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(60),
]

# Ceiling for fast open-breaker rejection. Must be well below socket_timeout (1.0s)
# to prove the breaker short-circuits rather than waiting on the dead dependency.
MAX_OPEN_REJECTION_LATENCY = 0.5


@pytest.fixture
async def provider(
    redis_container: RedisContainer,
) -> AsyncGenerator[RedisProvider]:
    """Yield a bounded-timeout Redis provider used as the protected dependency."""
    client = build_client(redis_container)
    provider = RedisProvider.from_client(client, own=True)
    async with provider:
        yield provider


async def test_breaker_trips_on_dead_dependency_then_recovers(
    redis_container: RedisContainer,
    provider: RedisProvider,
) -> None:
    """Breaker opens on the dying dependency, then half-open closes it.

    1. Healthy: a protected ping succeeds, breaker CLOSED.
    2. Pause Redis: pings raise slow timeout errors. After
       `error_threshold` of them the breaker OPENs.
    3. While OPEN: admission is rejected with a fast `CircuitBreakerError`
       instead of waiting on another connection timeout.
    4. Unpause + wait `reset_timeout`: the half-open probe succeeds and
       the breaker returns to CLOSED.
    """
    async with MemoryCircuitBreakerAdapter() as cb_backend:
        cb = CircuitBreaker.consecutive_count(
            f"dep-{uuid4().hex}",
            error_threshold=3,
            success_threshold=1,
            reset_timeout=2.0,
            half_open_capacity=1,
            backend=cb_backend,
        )

        async def ping() -> None:
            async with cb:
                await provider.client.ping()

        # 1. Healthy.
        await ping()
        assert cb.state is CircuitBreakerState.CLOSED

        with paused(redis_container):
            # 2. Drive failures until the breaker opens. Each failing call
            # pays one socket_timeout; the breaker opens at the threshold.
            for _ in range(3):
                with pytest.raises(Exception):  # noqa: PT011, B017 - timeout error
                    await ping()
            assert cb.state is CircuitBreakerState.OPEN

            # 3. OPEN rejects fast: a CircuitBreakerError, and far faster
            # than the socket timeout the dead dependency would impose.
            t0 = time.perf_counter()
            with pytest.raises(CircuitBreakerError):
                await ping()
            rejection_latency = time.perf_counter() - t0
            assert rejection_latency < MAX_OPEN_REJECTION_LATENCY, (
                "OPEN breaker must reject fast, not wait on the dead dependency"
            )

        # 4. Dependency back. Wait out the reset timeout, then the
        # half-open probe should succeed and close the breaker.
        await anyio.sleep(2.1)
        # The first call after reset_timeout is the half-open probe.
        await ping()
        assert cb.state is CircuitBreakerState.CLOSED, (
            "a successful half-open probe must close the breaker"
        )
