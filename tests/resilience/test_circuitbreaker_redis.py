"""Tests for Redis Circuit Breaker Adapter."""

import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.redis import RedisContainer

from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerState
from grelmicro.resilience.errors import CircuitBreakerError
from grelmicro.resilience.redis import RedisCircuitBreakerAdapter

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


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_try_acquire_closed_admits(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """A fresh breaker admits calls."""
    admitted = await backend.try_acquire(
        name="api", half_open_capacity=1, reset_timeout=5
    )
    assert admitted is True


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_record_error_opens_at_threshold(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """Reaching the error threshold transitions to OPEN with `opened_at` set."""
    for _ in range(2):
        state = await backend.record_error(
            name="api", error_threshold=3, reset_timeout=5
        )
        assert state.state is CircuitBreakerState.CLOSED

    state = await backend.record_error(
        name="api", error_threshold=3, reset_timeout=5
    )
    assert state.state is CircuitBreakerState.OPEN
    assert state.opened_at > 0
    assert state.consecutive_error_count == 0


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_open_rejects_until_reset_timeout_elapses(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """OPEN rejects calls until `reset_timeout`, then enters HALF_OPEN."""
    await backend.transition(
        name="api", desired=CircuitBreakerState.OPEN, reset_timeout=0.5
    )

    assert (
        await backend.try_acquire(
            name="api", half_open_capacity=1, reset_timeout=0.5
        )
        is False
    )

    await asyncio.sleep(0.6)

    assert (
        await backend.try_acquire(
            name="api", half_open_capacity=1, reset_timeout=0.5
        )
        is True
    )
    state = await backend.get_state(name="api")
    assert state.state is CircuitBreakerState.HALF_OPEN


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_half_open_admission_cap_enforced_globally(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """N concurrent acquires in HALF_OPEN never exceed `half_open_capacity`."""
    await backend.transition(
        name="api", desired=CircuitBreakerState.OPEN, reset_timeout=0.1
    )
    await asyncio.sleep(0.15)

    cap = 2
    results = await asyncio.gather(
        *(
            backend.try_acquire(
                name="api", half_open_capacity=cap, reset_timeout=0.1
            )
            for _ in range(10)
        )
    )

    assert sum(results) == cap


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_half_open_admits_next_probe_after_previous_completes(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`half_open_capacity=1` admits a new probe after the previous one completes."""
    await backend.transition(name="api", desired=CircuitBreakerState.HALF_OPEN)

    assert (
        await backend.try_acquire(
            name="api", half_open_capacity=1, reset_timeout=5
        )
        is True
    )
    await backend.record_success(
        name="api", success_threshold=10, reset_timeout=5
    )
    assert (
        await backend.try_acquire(
            name="api", half_open_capacity=1, reset_timeout=5
        )
        is True
    )


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_record_success_closes_half_open_at_threshold(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """HALF_OPEN transitions to CLOSED after `success_threshold` successes."""
    await backend.transition(name="api", desired=CircuitBreakerState.HALF_OPEN)

    state = await backend.record_success(
        name="api", success_threshold=2, reset_timeout=5
    )
    assert state.state is CircuitBreakerState.HALF_OPEN

    state = await backend.record_success(
        name="api", success_threshold=2, reset_timeout=5
    )
    assert state.state is CircuitBreakerState.CLOSED
    assert state.opened_at == 0
    assert state.consecutive_success_count == 0


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_transition_to_open_honors_custom_cool_down(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`transition(OPEN, reset_timeout=X)` cools down for X, ignoring try_acquire's arg."""
    await backend.transition(
        name="api", desired=CircuitBreakerState.OPEN, reset_timeout=0.2
    )

    assert (
        await backend.try_acquire(
            name="api", half_open_capacity=1, reset_timeout=60
        )
        is False
    )

    await asyncio.sleep(0.25)

    assert (
        await backend.try_acquire(
            name="api", half_open_capacity=1, reset_timeout=60
        )
        is True
    )


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_manual_transition_visible_via_get_state(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`transition` is immediately visible to subsequent `get_state` calls."""
    await backend.transition(
        name="api", desired=CircuitBreakerState.FORCED_OPEN
    )

    state = await backend.get_state(name="api")
    assert state.state is CircuitBreakerState.FORCED_OPEN

    admitted = await backend.try_acquire(
        name="api", half_open_capacity=1, reset_timeout=5
    )
    assert admitted is False


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_circuit_breaker_integration_end_to_end(
    backend: RedisCircuitBreakerAdapter,
) -> None:
    """`CircuitBreaker` wired to a shared backend opens after threshold errors."""

    class BoomError(Exception):
        pass

    cb = CircuitBreaker(
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

    cb_a = CircuitBreaker(
        "shared",
        error_threshold=2,
        success_threshold=1,
        reset_timeout=5,
        backend=backend,
    )
    cb_b = CircuitBreaker(
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
