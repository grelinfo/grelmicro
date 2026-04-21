"""Test RateLimiter implementation."""

from collections.abc import AsyncGenerator
from time import monotonic
from types import TracebackType
from typing import Any, Self
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from grelmicro._backends import BackendNotLoadedError
from grelmicro.resilience._backends import rate_limiter_backend_registry
from grelmicro.resilience._protocol import (
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.algorithms import GCRA, Algorithm, TokenBucket
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.memory import MemoryRateLimiterBackend
from grelmicro.resilience.ratelimiter import RateLimiter

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

LIMIT = 5
WINDOW = 60.0
CAPACITY = 5
REFILL_RATE = 1.0


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
def _sync_backend() -> MemoryRateLimiterBackend:
    """Create and register a memory backend for sync-only tests.

    Sync tests can't consume the async `_backend` fixture
    (pytest-anyio doesn't bridge them).
    """
    return MemoryRateLimiterBackend()


@pytest.fixture
def gcra_limiter() -> RateLimiter:
    """RateLimiter with GCRA."""
    return RateLimiter("test-gcra", algorithm=GCRA(limit=LIMIT, window=WINDOW))


@pytest.fixture
def token_bucket_limiter() -> RateLimiter:
    """RateLimiter with TokenBucket."""
    return RateLimiter(
        "test-tb",
        algorithm=TokenBucket(capacity=CAPACITY, refill_rate=REFILL_RATE),
    )


@pytest.fixture(params=["gcra", "token_bucket"])
def limiter(
    request: pytest.FixtureRequest,
    gcra_limiter: RateLimiter,
    token_bucket_limiter: RateLimiter,
) -> RateLimiter:
    """Parametrize tests across both algorithms."""
    return gcra_limiter if request.param == "gcra" else token_bucket_limiter


# --- Properties ---


@pytest.mark.usefixtures("_sync_backend")
def test_gcra_properties() -> None:
    """Test RateLimiter with GCRA properties."""
    # Arrange
    rl = RateLimiter("auth", algorithm=GCRA(limit=LIMIT, window=WINDOW))

    # Assert
    assert rl.config.name == "auth"
    assert isinstance(rl.config.algorithm, GCRA)
    assert rl.config.algorithm.limit == LIMIT
    assert rl.config.algorithm.window == WINDOW


@pytest.mark.usefixtures("_sync_backend")
def test_token_bucket_properties() -> None:
    """Test RateLimiter with TokenBucket properties."""
    # Arrange
    rl = RateLimiter(
        "api",
        algorithm=TokenBucket(capacity=CAPACITY, refill_rate=REFILL_RATE),
    )

    # Assert
    assert rl.config.name == "api"
    assert isinstance(rl.config.algorithm, TokenBucket)
    assert rl.config.algorithm.capacity == CAPACITY
    assert rl.config.algorithm.refill_rate == REFILL_RATE


# --- bind() called exactly once at __init__ ---


def test_bind_called_once_at_init() -> None:
    """Test bind() is called once at construction: zero runtime dispatch."""
    # Arrange
    algorithm = TokenBucket(capacity=CAPACITY, refill_rate=REFILL_RATE)
    strategy: Any = MagicMock(spec=RateLimiterStrategy)
    backend: Any = MagicMock()
    backend.bind = MagicMock(return_value=strategy)

    # Act
    RateLimiter("test", algorithm=algorithm, backend=backend)

    # Assert
    backend.bind.assert_called_once_with(algorithm)


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
async def test_acquire_remaining_decreases(limiter: RateLimiter) -> None:
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
async def test_acquire_independent_keys(limiter: RateLimiter) -> None:
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
async def test_acquire_cost_exceeds_remaining(limiter: RateLimiter) -> None:
    """Test cost rejects when not enough tokens remain."""
    # Arrange
    await limiter.acquire(key="user:1", cost=3)

    # Act
    result = await limiter.acquire(key="user:1", cost=3)

    # Assert
    assert result.allowed is False


@pytest.mark.usefixtures("_backend")
async def test_acquire_result_is_named_tuple(limiter: RateLimiter) -> None:
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
async def test_acquire_or_raise_allowed(limiter: RateLimiter) -> None:
    """Test acquire_or_raise returns result when within limit."""
    # Act
    result = await limiter.acquire_or_raise(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT - 1


@pytest.mark.usefixtures("_backend")
async def test_acquire_or_raise_exceeded(limiter: RateLimiter) -> None:
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
async def test_acquire_or_raise_with_cost(limiter: RateLimiter) -> None:
    """Test acquire_or_raise respects cost parameter."""
    # Arrange
    await limiter.acquire_or_raise(key="user:1", cost=LIMIT)

    # Act & Assert
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire_or_raise(key="user:1", cost=1)


# --- Backend not loaded ---


async def test_acquire_without_backend() -> None:
    """Test RateLimiter construction fails without backend."""
    # Act & Assert
    with pytest.raises(BackendNotLoadedError):
        RateLimiter("test", algorithm=GCRA(limit=LIMIT, window=WINDOW))


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
def test_invalid_gcra_config(limit: int, window: float) -> None:
    """Test non-positive limit or window raises ValidationError."""
    # Act & Assert
    with pytest.raises(ValidationError):
        GCRA(limit=limit, window=window)


@pytest.mark.parametrize(
    ("capacity", "refill_rate"),
    [
        (0, REFILL_RATE),
        (-1, REFILL_RATE),
        (CAPACITY, 0),
        (CAPACITY, -1),
    ],
)
def test_invalid_token_bucket_config(capacity: int, refill_rate: float) -> None:
    """Test non-positive capacity or refill_rate raises ValidationError."""
    # Act & Assert
    with pytest.raises(ValidationError):
        TokenBucket(capacity=capacity, refill_rate=refill_rate)


@pytest.mark.parametrize("cost", [0, -1, LIMIT + 1])
@pytest.mark.usefixtures("_backend")
async def test_invalid_cost(limiter: RateLimiter, cost: int) -> None:
    """Test cost outside 1..limit raises ValueError."""
    # Act & Assert
    with pytest.raises(ValueError, match="cost must be between"):
        await limiter.acquire(key="user:1", cost=cost)


# --- Algorithm resolution and deprecation ---


@pytest.mark.usefixtures("_sync_backend")
def test_legacy_ctor_emits_deprecation_warning() -> None:
    """Test RateLimiter(name, limit, window) emits DeprecationWarning."""
    # Act & Assert
    with pytest.warns(DeprecationWarning, match="algorithm=GCRA"):
        rl = RateLimiter("legacy", limit=LIMIT, window=WINDOW)

    # Assert: legacy resolves to GCRA
    assert isinstance(rl.config.algorithm, GCRA)
    assert rl.config.algorithm.limit == LIMIT


@pytest.mark.usefixtures("_sync_backend")
def test_both_algorithm_and_legacy_raises() -> None:
    """Test passing both algorithm= and limit= raises TypeError."""
    # Act & Assert
    with pytest.raises(TypeError, match="either"):
        RateLimiter(
            "bad",
            algorithm=GCRA(limit=LIMIT, window=WINDOW),
            limit=LIMIT,
        )


@pytest.mark.usefixtures("_sync_backend")
def test_no_algorithm_and_no_legacy_raises() -> None:
    """Test missing both algorithm= and legacy kwargs raises TypeError."""
    # Act & Assert
    with pytest.raises(TypeError, match="requires"):
        RateLimiter("bad")


@pytest.mark.usefixtures("_sync_backend")
def test_legacy_partial_kwargs_raises() -> None:
    """Test passing only one of limit/window raises an actionable TypeError."""
    # Act & Assert: message points to the migration path instead of
    # the generic "requires algorithm=" error.
    with pytest.raises(TypeError, match="must be provided together"):
        RateLimiter("bad", limit=LIMIT)
    with pytest.raises(TypeError, match="must be provided together"):
        RateLimiter("bad", window=WINDOW)


# --- Explicit backend override ---


async def test_explicit_backend_bypasses_registry() -> None:
    """Test backend= arg wins over registered default."""
    # Arrange: registered backend rejects everything
    rejected = MemoryRateLimiterBackend()
    my = MemoryRateLimiterBackend(auto_register=False)

    # Act: limiter uses the explicit backend
    rl = RateLimiter(
        "explicit",
        algorithm=GCRA(limit=LIMIT, window=WINDOW),
        backend=my,
    )

    # Assert: RateLimiter's backend is the explicit one
    assert rl._backend is my
    assert rl._backend is not rejected


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


# --- fail_open ---


class _FailingStrategy:
    """RateLimiterStrategy whose every method raises RuntimeError."""

    _error = RuntimeError("connection lost")

    async def acquire(
        self,
        *,
        key: str,  # noqa: ARG002
        cost: int,  # noqa: ARG002
    ) -> RateLimitResult:
        raise self._error

    async def peek(self, *, key: str) -> RateLimitResult:  # noqa: ARG002
        raise self._error

    async def reset(self, *, key: str) -> None:  # noqa: ARG002
        raise self._error


class _FailingBackend:
    """Backend whose bind() returns a strategy that always raises."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def bind(self, algorithm: Algorithm) -> RateLimiterStrategy:  # noqa: ARG002
        return _FailingStrategy()


@pytest.fixture
def failing_limiter() -> RateLimiter:
    """RateLimiter whose strategy raises on every call, fail_open=True."""
    return RateLimiter(
        "failing",
        algorithm=GCRA(limit=LIMIT, window=WINDOW),
        backend=_FailingBackend(),
        fail_open=True,
    )


@pytest.fixture
def failing_limiter_strict() -> RateLimiter:
    """RateLimiter whose strategy raises; fail_open=False."""
    return RateLimiter(
        "failing-strict",
        algorithm=GCRA(limit=LIMIT, window=WINDOW),
        backend=_FailingBackend(),
        fail_open=False,
    )


async def test_fail_open_acquire(
    failing_limiter: RateLimiter,
) -> None:
    """Test fail_open returns allowed result on backend error."""
    # Act
    result = await failing_limiter.acquire(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.limit == LIMIT
    assert result.remaining == LIMIT


async def test_fail_open_false_acquire_propagates_error(
    failing_limiter_strict: RateLimiter,
) -> None:
    """Test fail_open=False propagates backend error on acquire."""
    # Act & Assert
    with pytest.raises(RuntimeError, match="connection lost"):
        await failing_limiter_strict.acquire(key="user:1")


async def test_fail_open_peek(
    failing_limiter: RateLimiter,
) -> None:
    """Test fail_open returns allowed result on backend peek error."""
    # Act
    result = await failing_limiter.peek(key="user:1")

    # Assert
    assert result.allowed is True
    assert result.remaining == LIMIT


async def test_fail_open_false_peek_propagates_error(
    failing_limiter_strict: RateLimiter,
) -> None:
    """Test fail_open=False propagates backend error on peek."""
    # Act & Assert
    with pytest.raises(RuntimeError, match="connection lost"):
        await failing_limiter_strict.peek(key="user:1")


async def test_fail_open_reset(
    failing_limiter: RateLimiter,
) -> None:
    """Test fail_open silently ignores backend reset error."""
    # Act (should not raise)
    await failing_limiter.reset(key="user:1")


async def test_fail_open_false_reset_propagates_error(
    failing_limiter_strict: RateLimiter,
) -> None:
    """Test fail_open=False propagates backend error on reset."""
    # Act & Assert
    with pytest.raises(RuntimeError, match="connection lost"):
        await failing_limiter_strict.reset(key="user:1")


async def test_fail_open_acquire_or_raise(
    failing_limiter: RateLimiter,
) -> None:
    """Test fail_open on acquire_or_raise returns allowed on backend error."""
    # Act
    result = await failing_limiter.acquire_or_raise(key="user:1")

    # Assert
    assert result.allowed is True


async def test_gcra_strategy_evicts_expired_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _MemoryGCRA eviction drops keys whose TAT has passed."""
    # Arrange
    monkeypatch.setattr("grelmicro.resilience.memory._EVICTION_THRESHOLD", 1)
    backend = MemoryRateLimiterBackend(auto_register=False)
    limiter = RateLimiter(
        "evict",
        algorithm=GCRA(limit=LIMIT, window=WINDOW),
        backend=backend,
    )
    # Seed two fully-expired GCRA entries (TAT in the distant past).
    backend._gcra_state["evict:stale_a"] = 0.0
    backend._gcra_state["evict:stale_b"] = 0.0

    # Act: next acquire pushes past the threshold and evicts.
    await limiter.acquire(key="active")

    # Assert: stale entries are gone.
    assert "evict:stale_a" not in backend._gcra_state
    assert "evict:stale_b" not in backend._gcra_state


async def test_token_bucket_strategy_evicts_full_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test _MemoryTokenBucket eviction drops keys at full capacity."""
    # Arrange
    monkeypatch.setattr("grelmicro.resilience.memory._EVICTION_THRESHOLD", 2)
    backend = MemoryRateLimiterBackend(auto_register=False)
    limiter = RateLimiter(
        "evict",
        algorithm=TokenBucket(capacity=CAPACITY, refill_rate=1),
        backend=backend,
    )
    # Seed two entries that are fully refilled (so eviction can drop them).
    past = monotonic() - 10000.0
    backend._token_bucket_state["evict:idle_a"] = (CAPACITY, past)
    backend._token_bucket_state["evict:idle_b"] = (CAPACITY, past)

    # Act
    await limiter.acquire(key="active")

    # Assert
    assert "evict:idle_a" not in backend._token_bucket_state
    assert "evict:idle_b" not in backend._token_bucket_state


@pytest.mark.usefixtures("_backend")
async def test_fail_open_still_rejects_when_limit_exceeded() -> None:
    """Test fail_open does not bypass legitimate rate limit rejections."""
    # Arrange
    limiter = RateLimiter(
        "fo_reject",
        algorithm=GCRA(limit=LIMIT, window=WINDOW),
        fail_open=True,
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
        "fo_raise",
        algorithm=GCRA(limit=LIMIT, window=WINDOW),
        fail_open=True,
    )
    for _ in range(LIMIT):
        await limiter.acquire(key="user:1")

    # Act & Assert
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire_or_raise(key="user:1")
