"""Tests for SQLite Circuit Breaker Adapter."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerState,
    CircuitBreakerStrategy,
    ConsecutiveCountConfig,
)
from grelmicro.resilience.circuitbreaker.sqlite import (
    SQLiteCircuitBreakerAdapter,
)
from grelmicro.resilience.errors import CircuitBreakerError

pytestmark = [pytest.mark.timeout(5)]

PATH = "/tmp/test.db"  # noqa: S108


# --- Construction and wiring unit tests ---


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = SQLiteProvider(PATH)

    backend = SQLiteCircuitBreakerAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the backend builds its own from env vars."""
    monkeypatch.setenv("SQLITE_PATH", PATH)

    backend = SQLiteCircuitBreakerAdapter()

    assert backend.provider.path == PATH
    assert backend._owns_provider is True


def test_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prefix=` is stored on the backend."""
    monkeypatch.setenv("SQLITE_PATH", PATH)

    backend = SQLiteCircuitBreakerAdapter(prefix="myapp:")

    assert backend._prefix == "myapp:"
    assert backend._key_prefix == "myapp:cb:"


def test_invalid_table_name_raises() -> None:
    """An invalid SQL identifier is rejected."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        SQLiteCircuitBreakerAdapter(table_name="bad name;")


def test_is_shared() -> None:
    """`SQLiteCircuitBreakerAdapter.is_shared` is True."""
    assert SQLiteCircuitBreakerAdapter.is_shared is True


def test_rebind_provider_borrows_it() -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    backend = SQLiteCircuitBreakerAdapter(provider=SQLiteProvider("a.db"))
    other = SQLiteProvider("b.db")

    backend._rebind_provider(other)

    assert backend.provider is other
    assert backend._owns_provider is False


async def test_owned_provider_is_opened_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When owned, the adapter opens and closes its provider itself."""
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "owned.db"))
    backend = SQLiteCircuitBreakerAdapter()
    assert backend._owns_provider is True

    async with backend:
        strategy = backend.bind(
            name="api",
            config=ConsecutiveCountConfig(
                error_threshold=1,
                success_threshold=1,
                reset_timeout=1,
                half_open_capacity=1,
            ),
        )
        assert await strategy.try_acquire() is True

    with pytest.raises(Exception, match="outside of the context manager"):
        _ = backend.provider.client


def test_bind_rejects_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bind` raises NotImplementedError on an unknown algorithm kind."""
    monkeypatch.setenv("SQLITE_PATH", PATH)
    backend = SQLiteCircuitBreakerAdapter()

    class Fake:
        kind = "failure_rate"

    with pytest.raises(NotImplementedError, match="failure_rate"):
        backend.bind(name="x", config=Fake())  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]


# --- Behavior tests against a temp file (no server required) ---


@pytest.fixture
async def backend(
    tmp_path: Path,
) -> AsyncGenerator[SQLiteCircuitBreakerAdapter]:
    """SQLite circuit breaker adapter bound to a clean temp file per test."""
    path = tmp_path / "cb.db"
    provider = SQLiteProvider(str(path))
    async with (
        provider,
        SQLiteCircuitBreakerAdapter(provider=provider) as adapter,
    ):
        yield adapter


def _bind(
    backend: SQLiteCircuitBreakerAdapter,
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


async def test_try_acquire_closed_admits(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A fresh breaker admits calls."""
    strategy = _bind(backend)
    assert await strategy.try_acquire() is True


async def test_record_error_opens_at_threshold(
    backend: SQLiteCircuitBreakerAdapter,
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


async def test_open_rejects_until_reset_timeout_elapses(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """OPEN rejects calls until `reset_timeout`, then enters HALF_OPEN."""
    strategy = _bind(backend, reset_timeout=0.5)
    await strategy.transition(desired=CircuitBreakerState.OPEN)

    assert await strategy.try_acquire() is False

    await asyncio.sleep(0.6)

    assert await strategy.try_acquire() is True
    snapshot = await strategy.get_snapshot()
    assert snapshot.state is CircuitBreakerState.HALF_OPEN


async def test_half_open_admission_cap_enforced_globally(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """N concurrent acquires in HALF_OPEN never exceed `half_open_capacity`."""
    cap = 2
    strategy = _bind(backend, half_open_capacity=cap, reset_timeout=0.1)
    await strategy.transition(desired=CircuitBreakerState.OPEN)
    await asyncio.sleep(0.15)

    results = await asyncio.gather(*(strategy.try_acquire() for _ in range(10)))

    assert sum(results) == cap


async def test_record_success_closes_half_open_at_threshold(
    backend: SQLiteCircuitBreakerAdapter,
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


async def test_half_open_failure_reopens(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A failure in HALF_OPEN with error_threshold=1 reopens the breaker."""
    strategy = _bind(backend, error_threshold=1)
    await strategy.transition(desired=CircuitBreakerState.HALF_OPEN)

    snapshot = await strategy.record_outcome(success=False)

    assert snapshot.state is CircuitBreakerState.OPEN
    assert snapshot.opened_at > 0


async def test_transition_to_open_honors_custom_cool_down(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """`transition(OPEN, cool_down=X)` cools down for X, ignoring reset_timeout."""
    strategy = _bind(backend, reset_timeout=60)
    await strategy.transition(desired=CircuitBreakerState.OPEN, cool_down=0.2)

    assert await strategy.try_acquire() is False

    await asyncio.sleep(0.25)

    assert await strategy.try_acquire() is True


async def test_forced_open_rejects(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """FORCED_OPEN rejects calls and ignores recorded outcomes."""
    strategy = _bind(backend)
    await strategy.transition(desired=CircuitBreakerState.FORCED_OPEN)

    assert await strategy.try_acquire() is False

    snapshot = await strategy.record_outcome(success=True)
    assert snapshot.state is CircuitBreakerState.FORCED_OPEN


async def test_forced_closed_admits(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """FORCED_CLOSED admits calls and ignores recorded errors."""
    strategy = _bind(backend, error_threshold=1)
    await strategy.transition(desired=CircuitBreakerState.FORCED_CLOSED)

    assert await strategy.try_acquire() is True

    snapshot = await strategy.record_outcome(success=False)
    assert snapshot.state is CircuitBreakerState.FORCED_CLOSED


async def test_manual_transition_visible_via_get_snapshot(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """`transition` is immediately visible to subsequent `get_snapshot` calls."""
    strategy = _bind(backend)
    await strategy.transition(desired=CircuitBreakerState.FORCED_OPEN)

    snapshot = await strategy.get_snapshot()
    assert snapshot.state is CircuitBreakerState.FORCED_OPEN

    assert await strategy.try_acquire() is False


async def test_circuit_breaker_end_to_end(
    backend: SQLiteCircuitBreakerAdapter,
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


async def test_closed_success_stays_closed(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A success in CLOSED resets the error count and stays closed."""
    strategy = _bind(backend)

    snapshot = await strategy.record_outcome(success=True)

    assert snapshot.state is CircuitBreakerState.CLOSED
    assert snapshot.consecutive_error_count == 0


async def test_half_open_success_below_threshold_stays_half_open(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A HALF_OPEN success short of the threshold releases its admit slot."""
    strategy = _bind(
        backend, success_threshold=2, half_open_capacity=2, reset_timeout=0.1
    )
    await strategy.transition(desired=CircuitBreakerState.OPEN)
    await asyncio.sleep(0.15)
    assert await strategy.try_acquire() is True  # consumes one admit slot

    snapshot = await strategy.record_outcome(success=True)

    assert snapshot.state is CircuitBreakerState.HALF_OPEN
    assert snapshot.consecutive_success_count == 1


async def test_half_open_failure_releases_admit_slot(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A HALF_OPEN failure short of the threshold releases its admit slot."""
    strategy = _bind(
        backend, error_threshold=2, half_open_capacity=2, reset_timeout=0.1
    )
    await strategy.transition(desired=CircuitBreakerState.OPEN)
    await asyncio.sleep(0.15)
    assert await strategy.try_acquire() is True  # consumes one admit slot

    snapshot = await strategy.record_outcome(success=False)

    assert snapshot.state is CircuitBreakerState.HALF_OPEN
    assert snapshot.consecutive_error_count == 1


async def test_try_acquire_rolls_back_on_error(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A failing `try_acquire` rolls back and re-raises."""
    strategy = _bind(backend)
    conn = backend.provider.client
    await conn.execute("DROP TABLE grelmicro_circuit_breaker;")

    with pytest.raises(Exception, match="no such table"):
        await strategy.try_acquire()

    assert conn.in_transaction is False


async def test_record_outcome_rolls_back_on_error(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A failing `record_outcome` rolls back and re-raises."""
    strategy = _bind(backend)
    conn = backend.provider.client
    await conn.execute("DROP TABLE grelmicro_circuit_breaker;")

    with pytest.raises(Exception, match="no such table"):
        await strategy.record_outcome(success=False)

    assert conn.in_transaction is False


async def test_transition_rolls_back_on_error(
    backend: SQLiteCircuitBreakerAdapter,
) -> None:
    """A failing `transition` rolls back and re-raises."""
    strategy = _bind(backend)
    conn = backend.provider.client
    await conn.execute("DROP TABLE grelmicro_circuit_breaker;")

    with pytest.raises(Exception, match="no such table"):
        await strategy.transition(desired=CircuitBreakerState.OPEN)

    assert conn.in_transaction is False


async def test_two_breakers_share_state(
    backend: SQLiteCircuitBreakerAdapter,
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
