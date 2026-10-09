"""Tests for Rate Limiter Backends (parametrized across backends x algorithms)."""

import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer

from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
)
from grelmicro.resilience.ratelimiter import (
    RateLimiterConfig,
    SlidingWindowConfig,
    TokenBucketConfig,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.resilience.ratelimiter.postgres import PostgresRateLimiterAdapter
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter

pytestmark = [pytest.mark.timeout(30)]

LIMIT = 5
WINDOW = 60
CAPACITY = 5
REFILL_RATE = 0.1  # slow enough to not refill between assertions


# --- Fixtures (parametrized across backends + algorithms) ---


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("redis", marks=[pytest.mark.integration]),
        pytest.param("postgres", marks=[pytest.mark.integration]),
    ],
    scope="module",
)
def backend_name(request: pytest.FixtureRequest) -> str:
    """Backend name."""
    return request.param


@pytest.fixture(scope="module")
def container(
    backend_name: str,
) -> Generator[DockerContainer | None]:
    """Docker container (only for Redis and Postgres)."""
    if backend_name == "redis":
        with RedisContainer() as redis_container:
            yield redis_container
    elif backend_name == "postgres":
        with PostgresContainer() as pg_container:
            yield pg_container
    else:
        yield None


@pytest.fixture(scope="module")
async def backend(
    backend_name: str, container: DockerContainer | None
) -> AsyncGenerator[RateLimiterBackend]:
    """Rate limiter backend instance."""
    if backend_name == "redis" and container:
        port = container.get_exposed_port(6379)
        async with RedisRateLimiterAdapter(
            provider=RedisProvider(f"redis://localhost:{port}/0"),
            prefix="test:",
        ) as redis_backend:
            yield redis_backend
    elif backend_name == "postgres" and container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with (
            provider,
            PostgresRateLimiterAdapter(
                provider=provider, prefix="test:"
            ) as pg_backend,
        ):
            yield pg_backend
    elif backend_name == "sqlite":
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = SQLiteProvider(str(Path(tmpdir) / "rate_limit.db"))
            async with (
                provider,
                provider.ratelimiter_backend(prefix="test:") as sqlite_backend,
            ):
                yield sqlite_backend
    elif backend_name == "memory":
        async with MemoryRateLimiterAdapter() as memory_backend:
            yield memory_backend


@pytest.fixture(params=["sliding_window", "token_bucket"])
def config(request: pytest.FixtureRequest) -> RateLimiterConfig:
    """Algorithm config instance (parametrized)."""
    if request.param == "sliding_window":
        return SlidingWindowConfig(limit=LIMIT, window=WINDOW)
    return TokenBucketConfig(capacity=CAPACITY, refill_rate=REFILL_RATE)


@pytest.fixture
def strategy(
    backend: RateLimiterBackend, config: RateLimiterConfig
) -> RateLimiterStrategy:
    """Strategy produced by ``backend.bind(config)``."""
    return backend.bind(config)


# --- Shared tests (run against all backend x algorithm combinations) ---


async def test_acquire_allowed(strategy: RateLimiterStrategy) -> None:
    """Test acquire returns allowed result within limit."""
    # Act
    result = await strategy.acquire(key="allowed", cost=1)

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT - 1
    assert result.retry_after == 0.0
    assert result.reset_after > 0.0


async def test_acquire_rejected(strategy: RateLimiterStrategy) -> None:
    """Test acquire returns rejected when limit exceeded."""
    # Arrange
    for _ in range(LIMIT):
        await strategy.acquire(key="rejected", cost=1)

    # Act
    result = await strategy.acquire(key="rejected", cost=1)

    # Assert
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after > 0.0


async def test_acquire_independent_keys(strategy: RateLimiterStrategy) -> None:
    """Test different keys are independent."""
    # Arrange
    for _ in range(LIMIT):
        await strategy.acquire(key="key_a", cost=1)

    # Act
    result = await strategy.acquire(key="key_b", cost=1)

    # Assert
    assert result.allowed is True


async def test_acquire_cost(strategy: RateLimiterStrategy) -> None:
    """Test cost parameter consumes multiple tokens."""
    # Act
    result = await strategy.acquire(key="cost", cost=3)

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 3


# --- peek ---


async def test_peek_fresh_key(strategy: RateLimiterStrategy) -> None:
    """Test peek on a fresh key shows full quota."""
    # Act
    result = await strategy.peek(key="peek_fresh")

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT
    assert result.retry_after == 0.0


async def test_peek_does_not_consume(strategy: RateLimiterStrategy) -> None:
    """Test peek does not consume tokens."""
    # Arrange
    await strategy.acquire(key="peek_no_consume", cost=1)

    # Act
    result1 = await strategy.peek(key="peek_no_consume")
    result2 = await strategy.peek(key="peek_no_consume")

    # Assert
    assert result1.remaining == result2.remaining
    assert result1.allowed is True


async def test_peek_after_exhaustion(strategy: RateLimiterStrategy) -> None:
    """Test peek returns not allowed when limit exhausted."""
    # Arrange
    for _ in range(LIMIT):
        await strategy.acquire(key="peek_exhausted", cost=1)

    # Act
    result = await strategy.peek(key="peek_exhausted")

    # Assert
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after > 0.0


# --- reset ---


async def test_reset_restores_quota(strategy: RateLimiterStrategy) -> None:
    """Test reset restores full quota for a key."""
    # Arrange
    for _ in range(LIMIT):
        await strategy.acquire(key="reset_restore", cost=1)

    # Act
    await strategy.reset(key="reset_restore")
    result = await strategy.acquire(key="reset_restore", cost=1)

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 1


async def test_reset_nonexistent_key(strategy: RateLimiterStrategy) -> None:
    """Test reset on a nonexistent key is a no-op."""
    # Act (should not raise)
    await strategy.reset(key="reset_nonexistent")


async def test_reset_only_affects_given_key(
    strategy: RateLimiterStrategy,
) -> None:
    """Test reset of one key leaves other keys untouched.

    Regression: the Memory backend's per-algorithm state maps and
    the Redis backend's per-algorithm key prefixes must not leak
    across keys.
    """
    # Arrange
    await strategy.acquire(key="reset_iso_a", cost=1)
    await strategy.acquire(key="reset_iso_b", cost=1)

    # Act
    await strategy.reset(key="reset_iso_a")

    # Assert: key A is restored to full quota, key B is unchanged.
    result_a = await strategy.peek(key="reset_iso_a")
    result_b = await strategy.peek(key="reset_iso_b")
    assert result_a.remaining == LIMIT
    assert result_b.remaining == LIMIT - 1


# --- Sliding window arithmetic (every backend, independent of timing) ---

HOUR = 3600
HALF_HOUR = 1800.0
SLOW = 1.0
"""Seconds a test may take between two calls without changing its result."""


async def test_sliding_window_whole_limit_reports_its_slots(
    backend: RateLimiterBackend,
) -> None:
    """Spending the whole limit reports the slots, truncated to microseconds."""
    # Arrange: 187 slots of 3_000_000 // 187 = 16_042 microseconds each.
    limit = 187
    strategy = backend.bind(SlidingWindowConfig(limit=limit, window=3))

    # Act
    result = await strategy.acquire(key="sw_slots", cost=limit)

    # Assert
    assert result.allowed is True
    assert result.remaining == 0
    assert result.reset_after == 2.999854  # noqa: PLR2004


async def test_sliding_window_refusal_waits_for_the_next_slot(
    backend: RateLimiterBackend,
) -> None:
    """A refusal waits for the next slot and reports the window's reset."""
    # Arrange: two slots of half an hour, both spent.
    strategy = backend.bind(SlidingWindowConfig(limit=2, window=HOUR))
    spent = await strategy.acquire(key="sw_refusal", cost=2)

    # Act
    refused = await strategy.acquire(key="sw_refusal", cost=1)
    peeked = await strategy.peek(key="sw_refusal")

    # Assert
    assert spent.reset_after == HOUR
    assert refused.allowed is False
    assert HALF_HOUR - SLOW < refused.retry_after <= HALF_HOUR
    assert HOUR - SLOW < refused.reset_after <= HOUR
    assert peeked.allowed is False
    assert HALF_HOUR - SLOW < peeked.retry_after <= HALF_HOUR
    assert HOUR - SLOW < peeked.reset_after <= HOUR


async def test_sliding_window_counts_a_limit_past_32_bits(
    backend: RateLimiterBackend,
) -> None:
    """A limit past 2**31 reports its remaining requests exactly."""
    # Arrange: three billion per hour, slots of 1_200 nanoseconds.
    limit = 3_000_000_000
    strategy = backend.bind(SlidingWindowConfig(limit=limit, window=HOUR))

    # Act
    result = await strategy.acquire(key="sw_wide", cost=1)

    # Assert
    assert result.allowed is True
    assert result.remaining == limit - 1
