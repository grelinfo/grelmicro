"""Tests for Rate Limiter Backends (parametrized across implementations)."""

from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.redis import RedisContainer

from grelmicro.resilience._protocol import RateLimiterBackend
from grelmicro.resilience.memory import MemoryRateLimiterBackend
from grelmicro.resilience.redis import RedisRateLimiterBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(30)]

LIMIT = 5
WINDOW = 60.0


# --- Fixtures (parametrized across backends) ---


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """AnyIO Backend Module Scope."""
    return "asyncio"


@pytest.fixture(
    params=[
        "memory",
        pytest.param("redis", marks=[pytest.mark.integration]),
    ],
    scope="module",
)
def backend_name(request: pytest.FixtureRequest) -> str:
    """Backend name."""
    return request.param


@pytest.fixture(scope="module")
def container(
    backend_name: str,
) -> Generator[RedisContainer | None, None, None]:
    """Docker container (only for Redis)."""
    if backend_name == "redis":
        with RedisContainer() as redis_container:
            yield redis_container
    else:
        yield None


@pytest.fixture(scope="module")
async def backend(
    backend_name: str, container: RedisContainer | None
) -> AsyncGenerator[RateLimiterBackend]:
    """Rate limiter backend instance."""
    if backend_name == "redis" and container:
        port = container.get_exposed_port(6379)
        async with RedisRateLimiterBackend(
            f"redis://localhost:{port}/0",
            prefix="test:",
            auto_register=False,
        ) as redis_backend:
            yield redis_backend
    elif backend_name == "memory":
        async with MemoryRateLimiterBackend(
            auto_register=False,
        ) as memory_backend:
            yield memory_backend


# --- Shared tests (run against all backends) ---


async def test_acquire_allowed(
    backend: RateLimiterBackend,
) -> None:
    """Test acquire returns allowed result within limit."""
    # Act
    result = await backend.acquire(
        key="allowed", limit=LIMIT, window=WINDOW, cost=1
    )

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT - 1
    assert result.retry_after == 0.0
    assert result.reset_after > 0.0


async def test_acquire_rejected(
    backend: RateLimiterBackend,
) -> None:
    """Test acquire returns rejected when limit exceeded."""
    # Arrange
    for _ in range(LIMIT):
        await backend.acquire(
            key="rejected", limit=LIMIT, window=WINDOW, cost=1
        )

    # Act
    result = await backend.acquire(
        key="rejected", limit=LIMIT, window=WINDOW, cost=1
    )

    # Assert
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after > 0.0


async def test_acquire_independent_keys(
    backend: RateLimiterBackend,
) -> None:
    """Test different keys are independent."""
    # Arrange
    for _ in range(LIMIT):
        await backend.acquire(key="key_a", limit=LIMIT, window=WINDOW, cost=1)

    # Act
    result = await backend.acquire(
        key="key_b", limit=LIMIT, window=WINDOW, cost=1
    )

    # Assert
    assert result.allowed is True


async def test_acquire_cost(
    backend: RateLimiterBackend,
) -> None:
    """Test cost parameter consumes multiple tokens."""
    # Act
    result = await backend.acquire(
        key="cost", limit=LIMIT, window=WINDOW, cost=3
    )

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 3


# --- peek ---


async def test_peek_fresh_key(
    backend: RateLimiterBackend,
) -> None:
    """Test peek on a fresh key shows full quota."""
    # Act
    result = await backend.peek(key="peek_fresh", limit=LIMIT, window=WINDOW)

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT
    assert result.retry_after == 0.0


async def test_peek_does_not_consume(
    backend: RateLimiterBackend,
) -> None:
    """Test peek does not consume tokens."""
    # Arrange
    await backend.acquire(
        key="peek_no_consume", limit=LIMIT, window=WINDOW, cost=1
    )

    # Act
    result1 = await backend.peek(
        key="peek_no_consume", limit=LIMIT, window=WINDOW
    )
    result2 = await backend.peek(
        key="peek_no_consume", limit=LIMIT, window=WINDOW
    )

    # Assert
    assert result1.remaining == result2.remaining
    assert result1.allowed is True


async def test_peek_after_exhaustion(
    backend: RateLimiterBackend,
) -> None:
    """Test peek returns not allowed when limit exhausted."""
    # Arrange
    for _ in range(LIMIT):
        await backend.acquire(
            key="peek_exhausted", limit=LIMIT, window=WINDOW, cost=1
        )

    # Act
    result = await backend.peek(
        key="peek_exhausted", limit=LIMIT, window=WINDOW
    )

    # Assert
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after > 0.0


# --- reset ---


async def test_reset_restores_quota(
    backend: RateLimiterBackend,
) -> None:
    """Test reset restores full quota for a key."""
    # Arrange
    for _ in range(LIMIT):
        await backend.acquire(
            key="reset_restore", limit=LIMIT, window=WINDOW, cost=1
        )

    # Act
    await backend.reset(key="reset_restore")
    result = await backend.acquire(
        key="reset_restore", limit=LIMIT, window=WINDOW, cost=1
    )

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 1


async def test_reset_nonexistent_key(
    backend: RateLimiterBackend,
) -> None:
    """Test reset on a nonexistent key is a no-op."""
    # Act (should not raise)
    await backend.reset(key="reset_nonexistent")


async def test_reset_independent_keys(
    backend: RateLimiterBackend,
) -> None:
    """Test reset only affects the specified key."""
    # Arrange
    await backend.acquire(
        key="reset_indep_a", limit=LIMIT, window=WINDOW, cost=1
    )
    await backend.acquire(
        key="reset_indep_b", limit=LIMIT, window=WINDOW, cost=1
    )

    # Act
    await backend.reset(key="reset_indep_a")

    # Assert: key A is reset, key B is unchanged
    result_a = await backend.peek(
        key="reset_indep_a", limit=LIMIT, window=WINDOW
    )
    result_b = await backend.peek(
        key="reset_indep_b", limit=LIMIT, window=WINDOW
    )
    assert result_a.remaining == LIMIT
    assert result_b.remaining == LIMIT - 1
