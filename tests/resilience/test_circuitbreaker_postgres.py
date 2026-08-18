"""Tests for Postgres Circuit Breaker Adapter."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.postgres import PostgresContainer

from grelmicro.errors import SettingsValidationError
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
        backend.bind(name="x", config=Fake())  # ty: ignore[invalid-argument-type]


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

    # A long cool-down makes the rejection assert independent of scheduling:
    # a stalled runner cannot let the window elapse between the two calls.
    await strategy.transition(desired=CircuitBreakerState.OPEN, cool_down=60)
    assert await strategy.try_acquire() is False

    # Re-open with a short cool-down and wait several times past it, so the
    # elapse assert has margin instead of racing a 0.1s gap.
    await strategy.transition(desired=CircuitBreakerState.OPEN, cool_down=0.1)
    await asyncio.sleep(0.5)

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
    await asyncio.sleep(0.5)  # 5x the 0.1s cool-down, not a 0.05s race

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


SWEEP_LIMIT = 4
"""Rows a bounded sweep is allowed to delete in tests."""

SWEEP_SURVIVORS = 6
"""Rows left behind once the bounded sweep hits its limit."""


async def _row_count(backend: PostgresCircuitBreakerAdapter) -> int:
    return await backend.provider.client.fetchval(
        "SELECT count(*) FROM grelmicro_circuit_breaker;"
    )


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_recovered_circuit_stores_nothing(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """Closing on recovery deletes the row, since absence means CLOSED."""
    strategy = _bind(
        backend,
        name="recover",
        error_threshold=1,
        success_threshold=1,
        reset_timeout=0.01,
    )
    await strategy.try_acquire()
    await strategy.record_outcome(success=False)
    assert await _row_count(backend) == 1

    await asyncio.sleep(0.05)
    await strategy.try_acquire()
    snapshot = await strategy.record_outcome(success=True)

    assert snapshot.state is CircuitBreakerState.CLOSED
    assert await _row_count(backend) == 0


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_reset_stores_nothing(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """A manual reset deletes the row rather than storing a CLOSED one."""
    strategy = _bind(backend, name="reset", error_threshold=1)
    await strategy.try_acquire()
    await strategy.record_outcome(success=False)

    await strategy.transition(desired=CircuitBreakerState.CLOSED)

    assert await _row_count(backend) == 0
    assert (await strategy.get_snapshot()).state is CircuitBreakerState.CLOSED


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_stale_circuit_reads_as_closed(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """A circuit nobody has touched for its lifetime starts clean."""
    strategy = _bind(backend, name="stale", error_threshold=2)
    await strategy.try_acquire()
    await strategy.record_outcome(success=False)
    await backend.provider.client.execute(
        "UPDATE grelmicro_circuit_breaker SET updated_at = 0;"
    )

    snapshot = await strategy.record_outcome(success=False)

    # Counters restarted from one instead of reaching the threshold.
    assert snapshot.state is CircuitBreakerState.CLOSED
    assert snapshot.consecutive_error_count == 1


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_cleanup_deletes_only_expired_unforced_rows(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """The sweep reclaims stale circuits and never an operator hold."""
    stale = _bind(backend, name="stale", error_threshold=5)
    forced = _bind(backend, name="forced")
    fresh = _bind(backend, name="fresh", error_threshold=5)
    await stale.record_outcome(success=False)
    await forced.transition(desired=CircuitBreakerState.FORCED_OPEN)
    await fresh.record_outcome(success=False)
    await backend.provider.client.execute(
        "UPDATE grelmicro_circuit_breaker SET updated_at = 0 "
        "WHERE name IN ('cb:stale', 'cb:forced');"
    )

    await backend.provider.client.execute(backend._cleanup_sql, 60.0, 100)

    names = [
        r["name"]
        for r in await backend.provider.client.fetch(
            "SELECT name FROM grelmicro_circuit_breaker ORDER BY name;"
        )
    ]

    assert names == ["cb:forced", "cb:fresh"]


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_cleanup_is_bounded(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """A sweep deletes at most the row limit it is given."""
    for index in range(10):
        await _bind(backend, name=f"t{index}").record_outcome(success=False)
    await backend.provider.client.execute(
        "UPDATE grelmicro_circuit_breaker SET updated_at = 0;"
    )

    await backend.provider.client.execute(
        backend._cleanup_sql, 60.0, SWEEP_LIMIT
    )

    assert await _row_count(backend) == SWEEP_SURVIVORS


def test_cleanup_interval_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive sweep interval is rejected at construction."""
    monkeypatch.setenv("POSTGRES_URL", URL)
    with pytest.raises(
        SettingsValidationError, match="cleanup_interval must be positive"
    ):
        PostgresCircuitBreakerAdapter(cleanup_interval=0)


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_janitor_sweeps_on_its_interval(
    container: PostgresContainer,
) -> None:
    """The background sweep reclaims stale rows without being called."""
    port = container.get_exposed_port(5432)
    provider = PostgresProvider(f"postgresql://test:test@localhost:{port}/test")
    async with (
        provider,
        PostgresCircuitBreakerAdapter(
            provider=provider, cleanup_interval=0.05
        ) as adapter,
    ):
        await provider.client.execute("TRUNCATE grelmicro_circuit_breaker;")
        await _bind(adapter, name="stale").record_outcome(success=False)
        await provider.client.execute(
            "UPDATE grelmicro_circuit_breaker SET updated_at = 0;"
        )

        for _ in range(40):
            await asyncio.sleep(0.05)
            if await _row_count(adapter) == 0:
                break

        assert await _row_count(adapter) == 0


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_janitor_survives_a_failing_sweep(
    container: PostgresContainer, caplog: pytest.LogCaptureFixture
) -> None:
    """A sweep error is logged and the loop keeps running."""
    port = container.get_exposed_port(5432)
    provider = PostgresProvider(f"postgresql://test:test@localhost:{port}/test")
    async with (
        provider,
        PostgresCircuitBreakerAdapter(
            provider=provider, cleanup_interval=0.05
        ) as adapter,
    ):
        adapter._cleanup_sql = "SELECT does_not_exist($1, $2);"
        with caplog.at_level(logging.WARNING, logger="grelmicro"):
            await asyncio.sleep(0.3)

        assert adapter._janitor_task is not None
        assert not adapter._janitor_task.done()
        assert "cleanup sweep failed" in caplog.text


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_no_janitor_when_cleanup_is_disabled(
    container: PostgresContainer,
) -> None:
    """`cleanup_interval=None` starts no sweep and closes cleanly."""
    port = container.get_exposed_port(5432)
    provider = PostgresProvider(f"postgresql://test:test@localhost:{port}/test")
    async with (
        provider,
        PostgresCircuitBreakerAdapter(
            provider=provider, cleanup_interval=None
        ) as adapter,
    ):
        assert adapter._janitor_task is None


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_get_snapshot_honours_the_lifetime(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """A read of a stale circuit reports CLOSED, like every other backend."""
    strategy = _bind(backend, name="stale", error_threshold=1)
    await strategy.record_outcome(success=False)
    assert (await strategy.get_snapshot()).state is CircuitBreakerState.OPEN

    await backend.provider.client.execute(
        "UPDATE grelmicro_circuit_breaker SET updated_at = 0;"
    )

    assert (await strategy.get_snapshot()).state is CircuitBreakerState.CLOSED


@pytest.mark.integration
@_INTEGRATION_TIMEOUT
async def test_get_snapshot_keeps_a_forced_circuit(
    backend: PostgresCircuitBreakerAdapter,
) -> None:
    """An operator hold survives the lifetime on the read path too."""
    strategy = _bind(backend, name="forced")
    await strategy.transition(desired=CircuitBreakerState.FORCED_OPEN)
    await backend.provider.client.execute(
        "UPDATE grelmicro_circuit_breaker SET updated_at = 0;"
    )

    snapshot = await strategy.get_snapshot()

    assert snapshot.state is CircuitBreakerState.FORCED_OPEN
