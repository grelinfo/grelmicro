"""Test RateLimiter implementation."""

from collections.abc import AsyncGenerator

import pytest

from grelmicro._backends import BackendNotLoadedError
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import RateLimitResult
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.memory import MemoryRateLimiterBackend
from grelmicro.resilience.ratelimiter import RateLimiter

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

LIMIT = 5
WINDOW = 60.0


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Reset the rate limiter backend registry between tests."""
    rate_limiter_backend_registry.reset()


@pytest.fixture
async def _backend() -> AsyncGenerator[MemoryRateLimiterBackend]:
    """Create and register a memory rate limiter backend."""
    async with MemoryRateLimiterBackend() as b:
        yield b


@pytest.fixture
def limiter() -> RateLimiter:
    """Create a rate limiter with default settings."""
    return RateLimiter("test", limit=LIMIT, window=WINDOW)


# --- Properties ---


def test_properties() -> None:
    """Test RateLimiter properties."""
    # Arrange
    rl = RateLimiter("auth", limit=LIMIT, window=WINDOW)

    # Assert
    assert rl.config.name == "auth"
    assert rl.config.limit == LIMIT
    assert rl.config.window == WINDOW


# --- acquire ---


@pytest.mark.usefixtures("_backend")
async def test_acquire_allowed(limiter: RateLimiter) -> None:
    """Test acquire returns allowed result within limit."""
    # Act
    result = await limiter.acquire(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT - 1
    assert result.retry_after == 0.0
    assert result.reset_after > 0.0


@pytest.mark.usefixtures("_backend")
async def test_acquire_remaining_decreases(
    limiter: RateLimiter,
) -> None:
    """Test remaining count decreases with each acquire."""
    # Act
    results = [await limiter.acquire(key="user:1") for _ in range(LIMIT)]

    # Assert
    assert [r.remaining for r in results] == [4, 3, 2, 1, 0]
    assert all(r.allowed for r in results)


@pytest.mark.usefixtures("_backend")
async def test_acquire_rejected_when_limit_exceeded(
    limiter: RateLimiter,
) -> None:
    """Test acquire returns rejected result when limit exceeded."""
    # Arrange
    for _ in range(LIMIT):
        await limiter.acquire(key="user:1")

    # Act
    result = await limiter.acquire(key="user:1")

    # Assert
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after > 0.0
    assert result.reset_after > 0.0


@pytest.mark.usefixtures("_backend")
async def test_acquire_independent_keys(
    limiter: RateLimiter,
) -> None:
    """Test different keys are rate limited independently."""
    # Arrange
    for _ in range(LIMIT):
        await limiter.acquire(key="user:1")

    # Act
    result = await limiter.acquire(key="user:2")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 1


@pytest.mark.usefixtures("_backend")
async def test_acquire_cost(limiter: RateLimiter) -> None:
    """Test cost parameter consumes multiple tokens."""
    # Act
    result = await limiter.acquire(key="user:1", cost=3)

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 3


@pytest.mark.usefixtures("_backend")
async def test_acquire_cost_exceeds_remaining(
    limiter: RateLimiter,
) -> None:
    """Test cost rejects when not enough tokens remain."""
    # Arrange
    await limiter.acquire(key="user:1", cost=3)

    # Act
    result = await limiter.acquire(key="user:1", cost=3)

    # Assert
    assert result.allowed is False
    assert result.remaining == 0


@pytest.mark.usefixtures("_backend")
async def test_acquire_result_is_named_tuple(
    limiter: RateLimiter,
) -> None:
    """Test RateLimitResult is an immutable NamedTuple."""
    # Act
    result = await limiter.acquire(key="user:1")

    # Assert
    assert isinstance(result, RateLimitResult)
    assert isinstance(result, tuple)
    with pytest.raises(AttributeError):
        result.allowed = False  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# --- acquire_or_raise ---


@pytest.mark.usefixtures("_backend")
async def test_acquire_or_raise_allowed(
    limiter: RateLimiter,
) -> None:
    """Test acquire_or_raise returns result when within limit."""
    # Act
    result = await limiter.acquire_or_raise(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 1


@pytest.mark.usefixtures("_backend")
async def test_acquire_or_raise_exceeded(
    limiter: RateLimiter,
) -> None:
    """Test acquire_or_raise raises RateLimitExceededError."""
    # Arrange
    for _ in range(LIMIT):
        await limiter.acquire(key="user:1")

    # Act & Assert
    with pytest.raises(RateLimitExceededError) as exc_info:
        await limiter.acquire_or_raise(key="user:1")

    assert exc_info.value.key == "user:1"
    assert exc_info.value.retry_after > 0.0
    assert "user:1" in str(exc_info.value)


@pytest.mark.usefixtures("_backend")
async def test_acquire_or_raise_with_cost(
    limiter: RateLimiter,
) -> None:
    """Test acquire_or_raise respects cost parameter."""
    # Arrange
    await limiter.acquire_or_raise(key="user:1", cost=LIMIT)

    # Act & Assert
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire_or_raise(key="user:1", cost=1)


# --- Backend not loaded ---


async def test_acquire_without_backend() -> None:
    """Test acquire raises BackendNotLoadedError without backend."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW)

    # Act & Assert
    with pytest.raises(BackendNotLoadedError):
        await limiter.acquire(key="user:1")


# --- Error class ---


def test_rate_limit_exceeded_error() -> None:
    """Test RateLimitExceededError attributes and message."""
    # Arrange
    retry_after = 12.5

    # Act
    error = RateLimitExceededError(key="192.168.1.1", retry_after=retry_after)

    # Assert
    assert error.key == "192.168.1.1"
    assert error.retry_after == retry_after
    assert "192.168.1.1" in str(error)
    assert "12.5" in str(error)
