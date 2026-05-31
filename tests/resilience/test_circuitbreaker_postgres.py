"""Tests for Postgres Circuit Breaker Adapter."""

import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.postgres import PostgresContainer

from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerState,
    CircuitBreakerStrategy,
    ConsecutiveCountConfig,
)
from grelmicro.resilience.circuitbreaker.postgres import (
    PostgresCircuitBreakerAdapter,
)
from grelmicro.resilience.errors import CircuitBreakerError

pytestmark = [pytest.mark.timeout(1)]

URL = "postgresql://test:test@test_host:5432/test"


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = PostgresProvider(URL)

    backend = PostgresCircuitBreakerAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    backend = PostgresCircuitBreakerAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prefix=` is stored on the backend."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    backend = PostgresCircuitBreakerAdapter(prefix="myapp:")

    assert backend._prefix == "myapp:"
    assert backend._key_prefix == "myapp:cb:"


def test_invalid_table_name_raises() -> None:
    """An invalid SQL identifier is rejected."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        PostgresCircuitBreakerAdapter(table_name="bad name;")


def test_is_shared() -> None:
    """`PostgresCircuitBreakerAdapter.is_shared` is True."""
    assert PostgresCircuitBreakerAdapter.is_shared is True


def test_bind_rejects_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bind` raises NotImplementedError on an unknown algorithm kind."""
    monkeypatch.setenv("POSTGRES_URL", URL)
    backend = PostgresCircuitBreakerAdapter()

    class Fake:
        kind = "failure_rate"

    with pytest.raises(NotImplementedError, match="failure_rate"):
        backend.bind(name="x", config=Fake())  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]


# --- Integration tests against a real Postgres container ---


_INTEGRATION_TIMEOUT = pytest.mark.timeout(30)


@pytest.fixture(scope="module")
def container() -> Generator[PostgresContainer, None, None]:
    """Docker container running Postgres."""
    with PostgresContainer() as pg_container:
        yield pg_container


@pytest.fixture
async def backend(
    container: PostgresContainer,
) -> AsyncGenerator[PostgresCircuitBreakerAdapter]:
    """Postgres circuit breaker adapter bound to a clean table per test."""
    port = container.get_exposed_port(5432)
    provider = PostgresProvider(f"postgresql://test:test@localhost:{port}/test")
    async with (
        provider,
        PostgresCircuitBreakerAdapter(provider=provider) as adapter,
    ):
        await provider.client.execute("TRUNCATE grelmicro_circuit_breaker;")
        yield adapter


def _bind(
    backend: PostgresCircuitBreakerAdapter,
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
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """A fresh breaker admits calls."""
    strategy = _bind(backend)
    assert await strategy.try_acquire() is True


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_record_error_opens_at_threshold(
    backend: PostgresCircuitBreakerAdapter,
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
    backend: PostgresCircuitBreakerAdapter,
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
    backend: PostgresCircuitBreakerAdapter,
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
async def test_record_success_closes_half_open_at_threshold(
    backend: PostgresCircuitBreakerAdapter,
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
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """`transition(OPEN, cool_down=X)` cools down for X, ignoring reset_timeout."""
    strategy = _bind(backend, reset_timeout=60)
    await strategy.transition(desired=CircuitBreakerState.OPEN, cool_down=0.2)

    assert await strategy.try_acquire() is False

    await asyncio.sleep(0.25)

    assert await strategy.try_acquire() is True


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_manual_transition_visible_via_get_snapshot(
    backend: PostgresCircuitBreakerAdapter,
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
    backend: PostgresCircuitBreakerAdapter,
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
    backend: PostgresCircuitBreakerAdapter,
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
