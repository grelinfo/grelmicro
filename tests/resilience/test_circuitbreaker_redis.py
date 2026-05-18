"""Tests for Redis Circuit Breaker Adapter."""

import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.redis import RedisContainer

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerState,
    CircuitBreakerStrategy,
    ConsecutiveCountConfig,
)
from grelmicro.resilience.circuitbreaker.redis import RedisCircuitBreakerAdapter
from grelmicro.resilience.errors import CircuitBreakerError

pytestmark = [pytest.mark.timeout(1)]

URL = "redis://:test_password@test_host:1234/0"


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = RedisProvider(URL)

    backend = RedisCircuitBreakerAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisCircuitBreakerAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prefix=` is stored on the backend."""
    monkeypatch.setenv("REDIS_URL", URL)

    backend = RedisCircuitBreakerAdapter(prefix="myapp:")

    assert backend._prefix == "myapp:"
    assert backend._key_prefix == "myapp:cb:"


def test_is_shared() -> None:
    """`RedisCircuitBreakerAdapter.is_shared` is True."""
    assert RedisCircuitBreakerAdapter.is_shared is True


def test_bind_rejects_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bind` raises NotImplementedError on an unknown algorithm kind."""
    monkeypatch.setenv("REDIS_URL", URL)
    backend = RedisCircuitBreakerAdapter()

    class Fake:
        kind = "failure_rate"

    with pytest.raises(NotImplementedError, match="failure_rate"):
        backend.bind(name="x", config=Fake())  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]


# --- Integration tests against a real Redis container ---


_INTEGRATION_TIMEOUT = pytest.mark.timeout(30)


@pytest.fixture(scope="module")
def container() -> Generator[RedisContainer, None, None]:
    """Docker container running Redis."""
    with RedisContainer() as redis_container:
        yield redis_container


@pytest.fixture
async def backend(
    container: RedisContainer,
) -> AsyncGenerator[RedisCircuitBreakerAdapter]:
    """Redis circuit breaker adapter bound to a fresh keyspace per test."""
    port = container.get_exposed_port(6379)
    provider = RedisProvider(f"redis://localhost:{port}/0")
    async with provider:
        await provider.client.flushdb()
        async with RedisCircuitBreakerAdapter(provider=provider) as adapter:
            yield adapter


def _bind(
    backend: RedisCircuitBreakerAdapter,
    *,
    name: str = "api",
    error_threshold: int = 3,
    success_threshold: int = 2,
    reset_timeout: float = 5,
    half_open_capacity: int = 1,
) -> CircuitBreakerStrategy:
    return backend.bind(
        name=name,
        config=ConsecutiveCountConfig(
            error_threshold=error_threshold,
            success_threshold=success_threshold,
            reset_timeout=reset_timeout,
            half_open_capacity=half_open_capacity,
        ),
    )


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_try_acquire_closed_admits(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """A fresh breaker admits calls."""
    strategy = _bind(backend)
    assert await strategy.try_acquire() is True


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_record_error_opens_at_threshold(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """Reaching the error threshold transitions to OPEN with `opened_at` set."""
    strategy = _bind(backend, error_threshold=3)
    for _ in range(2):
        snapshot = await strategy.record_outcome(success=False)
        assert snapshot.state is CircuitBreakerState.CLOSED

    snapshot = await strategy.record_outcome(success=False)
    assert snapshot.state is CircuitBreakerState.OPEN
    assert snapshot.opened_at > 0
    assert snapshot.consecutive_error_count == 0


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_open_rejects_until_reset_timeout_elapses(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """OPEN rejects calls until `reset_timeout`, then enters HALF_OPEN."""
    strategy = _bind(backend, reset_timeout=0.5)
    await strategy.transition(desired=CircuitBreakerState.OPEN)

    assert await strategy.try_acquire() is False

    await asyncio.sleep(0.6)

    assert await strategy.try_acquire() is True
    snapshot = await strategy.get_snapshot()
    assert snapshot.state is CircuitBreakerState.HALF_OPEN


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_half_open_admission_cap_enforced_globally(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """N concurrent acquires in HALF_OPEN never exceed `half_open_capacity`."""
    cap = 2
    strategy = _bind(backend, half_open_capacity=cap, reset_timeout=0.1)
    await strategy.transition(desired=CircuitBreakerState.OPEN)
    await asyncio.sleep(0.15)

    results = await asyncio.gather(*(strategy.try_acquire() for _ in range(10)))

    assert sum(results) == cap


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_half_open_admits_next_probe_after_previous_completes(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`half_open_capacity=1` admits a new probe after the previous one completes."""
    strategy = _bind(backend, half_open_capacity=1, success_threshold=10)
    await strategy.transition(desired=CircuitBreakerState.HALF_OPEN)

    assert await strategy.try_acquire() is True
    await strategy.record_outcome(success=True)
    assert await strategy.try_acquire() is True


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_record_success_closes_half_open_at_threshold(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """HALF_OPEN transitions to CLOSED after `success_threshold` successes."""
    strategy = _bind(backend, success_threshold=2)
    await strategy.transition(desired=CircuitBreakerState.HALF_OPEN)

    snapshot = await strategy.record_outcome(success=True)
    assert snapshot.state is CircuitBreakerState.HALF_OPEN

    snapshot = await strategy.record_outcome(success=True)
    assert snapshot.state is CircuitBreakerState.CLOSED
    assert snapshot.opened_at == 0
    assert snapshot.consecutive_success_count == 0


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_transition_to_open_honors_custom_cool_down(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`transition(OPEN, cool_down=X)` cools down for X, ignoring config.reset_timeout."""
    strategy = _bind(backend, reset_timeout=60)
    await strategy.transition(desired=CircuitBreakerState.OPEN, cool_down=0.2)

    assert await strategy.try_acquire() is False

    await asyncio.sleep(0.25)

    assert await strategy.try_acquire() is True


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_manual_transition_visible_via_get_snapshot(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`transition` is immediately visible to subsequent `get_snapshot` calls."""
    strategy = _bind(backend)
    await strategy.transition(desired=CircuitBreakerState.FORCED_OPEN)

    snapshot = await strategy.get_snapshot()
    assert snapshot.state is CircuitBreakerState.FORCED_OPEN

    assert await strategy.try_acquire() is False


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_circuit_breaker_integration_end_to_end(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`CircuitBreaker` wired to a shared backend opens after threshold errors."""

    class BoomError(Exception):
        pass

    cb = CircuitBreaker.consecutive_count(
        "payments",
        error_threshold=2,
        success_threshold=1,
        reset_timeout=5,
        backend=backend,
    )

    for _ in range(2):
        with pytest.raises(BoomError):
            async with cb:
                raise BoomError

    with pytest.raises(CircuitBreakerError):
        async with cb:
            pass

    assert cb.state is CircuitBreakerState.OPEN


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_two_breakers_share_state(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """Two `CircuitBreaker` instances with the same name see the same state."""

    class BoomError(Exception):
        pass

    cb_a = CircuitBreaker.consecutive_count(
        "shared",
        error_threshold=2,
        success_threshold=1,
        reset_timeout=5,
        backend=backend,
    )
    cb_b = CircuitBreaker.consecutive_count(
        "shared",
        error_threshold=2,
        success_threshold=1,
        reset_timeout=5,
        backend=backend,
    )

    for _ in range(2):
        with pytest.raises(BoomError):
            async with cb_a:
                raise BoomError

    with pytest.raises(CircuitBreakerError):
        async with cb_b:
            pass

    assert cb_b.state is CircuitBreakerState.OPEN
