"""Test RateLimiter implementation."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

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


async def test_acquire_evicts_expired_keys(
    limiter: RateLimiter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test lazy eviction removes expired keys when threshold exceeded."""
    # Arrange
    monkeypatch.setattr("grelmicro.resilience.memory._EVICTION_THRESHOLD", 2)
    async with MemoryRateLimiterBackend() as backend:
        backend._tats["test:expired1"] = 0.0
        backend._tats["test:expired2"] = 0.0
        backend._tats["test:expired3"] = 0.0

        # Act
        await limiter.acquire(key="user:1")

        # Assert
        assert "test:expired1" not in backend._tats
        assert "test:expired2" not in backend._tats
        assert "test:expired3" not in backend._tats


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


# --- Validation ---


@pytest.mark.parametrize(
    ("limit", "window"),
    [
        (0, WINDOW),
        (-1, WINDOW),
        (LIMIT, 0),
        (LIMIT, -1),
    ],
)
def test_invalid_config(limit: int, window: float) -> None:
    """Test non-positive limit or window raises ValidationError."""
    # Act & Assert
    with pytest.raises(ValidationError):
        RateLimiter("test", limit=limit, window=window)


@pytest.mark.parametrize("cost", [0, -1, LIMIT + 1])
@pytest.mark.usefixtures("_backend")
async def test_invalid_cost(limiter: RateLimiter, cost: int) -> None:
    """Test cost outside 1..limit raises ValueError."""
    # Act & Assert
    with pytest.raises(ValueError, match="cost must be between"):
        await limiter.acquire(key="user:1", cost=cost)


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


# --- peek ---


@pytest.mark.usefixtures("_backend")
async def test_peek_allowed(limiter: RateLimiter) -> None:
    """Test peek returns allowed result for fresh key."""
    # Act
    result = await limiter.peek(key="user:peek1")

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT
    assert result.retry_after == 0.0


@pytest.mark.usefixtures("_backend")
async def test_peek_does_not_consume(limiter: RateLimiter) -> None:
    """Test peek does not consume tokens."""
    # Act
    await limiter.peek(key="user:peek2")
    await limiter.peek(key="user:peek2")
    result = await limiter.acquire(key="user:peek2")

    # Assert
    assert result.remaining == LIMIT - 1


@pytest.mark.usefixtures("_backend")
async def test_peek_after_exhaustion(limiter: RateLimiter) -> None:
    """Test peek returns not allowed when limit exhausted."""
    # Arrange
    for _ in range(LIMIT):
        await limiter.acquire(key="user:peek3")

    # Act
    result = await limiter.peek(key="user:peek3")

    # Assert
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after > 0.0


@pytest.mark.usefixtures("_backend")
async def test_peek_reflects_remaining(limiter: RateLimiter) -> None:
    """Test peek remaining reflects consumed tokens."""
    # Arrange
    await limiter.acquire(key="user:peek4", cost=3)

    # Act
    result = await limiter.peek(key="user:peek4")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 3


async def test_peek_without_backend() -> None:
    """Test peek raises BackendNotLoadedError without backend."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW)

    # Act & Assert
    with pytest.raises(BackendNotLoadedError):
        await limiter.peek(key="user:1")


# --- reset ---


@pytest.mark.usefixtures("_backend")
async def test_reset_restores_quota(limiter: RateLimiter) -> None:
    """Test reset restores full quota for a key."""
    # Arrange
    for _ in range(LIMIT):
        await limiter.acquire(key="user:reset1")

    # Act
    await limiter.reset(key="user:reset1")
    result = await limiter.acquire(key="user:reset1")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 1


@pytest.mark.usefixtures("_backend")
async def test_reset_nonexistent_key(limiter: RateLimiter) -> None:
    """Test reset on a nonexistent key is a no-op."""
    # Act (should not raise)
    await limiter.reset(key="user:nonexistent")


@pytest.mark.usefixtures("_backend")
async def test_reset_independent_keys(limiter: RateLimiter) -> None:
    """Test reset only affects the specified key."""
    # Arrange
    await limiter.acquire(key="user:reset_a")
    await limiter.acquire(key="user:reset_b")

    # Act
    await limiter.reset(key="user:reset_a")

    # Assert
    result_a = await limiter.peek(key="user:reset_a")
    result_b = await limiter.peek(key="user:reset_b")
    assert result_a.remaining == LIMIT
    assert result_b.remaining == LIMIT - 1


async def test_reset_without_backend() -> None:
    """Test reset raises BackendNotLoadedError without backend."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW)

    # Act & Assert
    with pytest.raises(BackendNotLoadedError):
        await limiter.reset(key="user:1")


# --- fail_open ---


@pytest.fixture
def _failing_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register a backend where all methods raise RuntimeError."""
    backend = MemoryRateLimiterBackend()
    error = RuntimeError("connection lost")
    monkeypatch.setattr(backend, "acquire", AsyncMock(side_effect=error))
    monkeypatch.setattr(backend, "peek", AsyncMock(side_effect=error))
    monkeypatch.setattr(backend, "reset", AsyncMock(side_effect=error))


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_acquire() -> None:
    """Test fail_open returns allowed result on backend error."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=True)

    # Act
    result = await limiter.acquire(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_false_acquire_propagates_error() -> None:
    """Test fail_open=False propagates backend error on acquire."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=False)

    # Act & Assert
    with pytest.raises(RuntimeError, match="connection lost"):
        await limiter.acquire(key="user:1")


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_peek() -> None:
    """Test fail_open returns allowed result on backend peek error."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=True)

    # Act
    result = await limiter.peek(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_false_peek_propagates_error() -> None:
    """Test fail_open=False propagates backend error on peek."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=False)

    # Act & Assert
    with pytest.raises(RuntimeError, match="connection lost"):
        await limiter.peek(key="user:1")


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_reset() -> None:
    """Test fail_open silently ignores backend reset error."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=True)

    # Act (should not raise)
    await limiter.reset(key="user:1")


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_false_reset_propagates_error() -> None:
    """Test fail_open=False propagates backend error on reset."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=False)

    # Act & Assert
    with pytest.raises(RuntimeError, match="connection lost"):
        await limiter.reset(key="user:1")


@pytest.mark.usefixtures("_failing_backend")
async def test_fail_open_acquire_or_raise() -> None:
    """Test fail_open on acquire_or_raise returns allowed on backend error."""
    # Arrange
    limiter = RateLimiter("test", limit=LIMIT, window=WINDOW, fail_open=True)

    # Act
    result = await limiter.acquire_or_raise(key="user:1")

    # Assert
    assert result.allowed is True


@pytest.mark.usefixtures("_backend")
async def test_fail_open_still_rejects_when_limit_exceeded() -> None:
    """Test fail_open does not bypass legitimate rate limit rejections."""
    # Arrange
    limiter = RateLimiter(
        "fo_reject", limit=LIMIT, window=WINDOW, fail_open=True
    )
    for _ in range(LIMIT):
        await limiter.acquire(key="user:1")

    # Act
    result = await limiter.acquire(key="user:1")

    # Assert
    assert result.allowed is False


@pytest.mark.usefixtures("_backend")
async def test_fail_open_acquire_or_raise_still_raises_on_exceeded() -> None:
    """Test fail_open acquire_or_raise still raises on legitimate exceeded."""
    # Arrange
    limiter = RateLimiter(
        "fo_raise", limit=LIMIT, window=WINDOW, fail_open=True
    )
    for _ in range(LIMIT):
        await limiter.acquire(key="user:1")

    # Act & Assert
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire_or_raise(key="user:1")
