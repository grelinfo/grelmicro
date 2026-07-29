"""Test per-key circuits created by `CircuitBreaker.keyed`."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress

import pytest

from grelmicro import Grelmicro
from grelmicro._config import reconfigurable_instances
from grelmicro.resilience import CircuitBreaker, CircuitBreakerRegistry
from grelmicro.resilience.circuitbreaker import (
    CircuitBreakerError,
    CircuitBreakerState,
)
from grelmicro.resilience.circuitbreaker import memory as cb_memory
from grelmicro.resilience.circuitbreaker.consecutive_count import (
    ConsecutiveCountConfig,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)

KEYS = 50
"""Keys driven through a breaker in cardinality tests."""

MAXSIZE = 3
"""Resident-circuit budget used by the eviction tests."""

ERRORS = 2
"""Errors driven through a circuit in counter tests."""

UNCULLED = 20
"""Entries a single bounded cull must leave behind."""

DAY = 86400.0
"""The flat stored-state lifetime, in seconds."""

LONG_COOL_DOWN = 40000.0
"""A cool-down whose floor exceeds the flat lifetime."""


class SentinelError(Exception):
    """A sentinel error for testing purposes."""


@pytest.fixture
def backend() -> MemoryCircuitBreakerAdapter:
    """Construct the in-memory CB backend fixture (one per test)."""
    return MemoryCircuitBreakerAdapter()


@pytest.fixture(autouse=True)
async def _app(
    backend: MemoryCircuitBreakerAdapter,
) -> AsyncGenerator[Grelmicro]:
    """Open a `Grelmicro` app holding the in-memory CB backend."""
    async with Grelmicro(uses=[CircuitBreakerRegistry(backend)]) as micro:
        yield micro


async def trip(cb: CircuitBreaker, threshold: int) -> None:
    """Drive `threshold` consecutive errors through the circuit."""
    for _ in range(threshold):
        with suppress(SentinelError):
            async with cb:
                raise SentinelError


async def test_keys_trip_independently() -> None:
    """Errors on one key open that key only."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=2)

    await trip(cb.keyed("a"), 2)

    assert cb.keyed("a").state == CircuitBreakerState.OPEN
    assert cb.keyed("b").state == CircuitBreakerState.CLOSED
    with pytest.raises(CircuitBreakerError):
        async with cb.keyed("a"):
            pytest.fail("Expected not reached")
    async with cb.keyed("b"):
        pass


async def test_keyed_does_not_affect_unkeyed_circuit() -> None:
    """Tripping a key leaves the unkeyed circuit closed."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=2)

    await trip(cb.keyed("a"), 2)

    assert cb.state == CircuitBreakerState.CLOSED
    async with cb:
        pass


async def test_unkeyed_does_not_affect_keyed_circuit() -> None:
    """Tripping the unkeyed circuit leaves keys closed."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=2)

    await trip(cb, 2)

    assert cb.state == CircuitBreakerState.OPEN
    assert cb.keyed("a").state == CircuitBreakerState.CLOSED


async def test_keyed_is_memoized() -> None:
    """The same key returns the same circuit."""
    cb = CircuitBreaker.consecutive_count("upstream")

    assert cb.keyed("a") is cb.keyed("a")
    assert cb.keyed("a") is not cb.keyed("b")


async def test_keyed_identity() -> None:
    """A keyed circuit reports its composite name and its key."""
    cb = CircuitBreaker.consecutive_count("upstream")

    circuit = cb.keyed("acme")

    assert circuit.name == "upstream:acme"
    assert circuit.key == "acme"


async def test_keyed_state_is_stored_under_composite_name(
    backend: MemoryCircuitBreakerAdapter,
) -> None:
    """Backend state lands under `name:key`, not under `name`."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=1)

    await trip(cb.keyed("acme"), 1)

    assert "upstream:acme" in backend._states
    assert "upstream" not in backend._states


async def test_keyed_isolate_and_reset_are_per_key() -> None:
    """Manual control drives one key without touching the others."""
    cb = CircuitBreaker.consecutive_count("upstream")

    await cb.keyed("a").isolate()

    assert cb.keyed("a").state == CircuitBreakerState.FORCED_OPEN
    assert cb.keyed("b").state == CircuitBreakerState.CLOSED
    async with cb.keyed("b"):
        pass

    await cb.keyed("a").reset()

    assert cb.keyed("a").state == CircuitBreakerState.CLOSED


async def test_keyed_metrics_are_per_key() -> None:
    """Counters accumulate per key."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=5)

    await trip(cb.keyed("a"), ERRORS)
    async with cb.keyed("b"):
        pass

    assert cb.keyed("a").metrics().name == "upstream:a"
    assert cb.keyed("a").metrics().total_error_count == ERRORS
    assert cb.keyed("a").metrics().total_success_count == 0
    assert cb.keyed("b").metrics().total_error_count == 0
    assert cb.keyed("b").metrics().total_success_count == 1


async def test_emitted_metrics_are_attributed_to_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emitted metrics carry the breaker name, never the key."""
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker._emit.incr",
        lambda _name, **attributes: emitted.append(attributes),
    )
    cb = CircuitBreaker.consecutive_count("upstream")

    async with cb.keyed("tenant-1"):
        pass

    assert emitted
    assert all(a["circuit_breaker.name"] == "upstream" for a in emitted)


async def test_keyed_async_decorator() -> None:
    """A keyed circuit decorates an async function."""
    cb = CircuitBreaker.consecutive_count("upstream")

    @cb.keyed("a")
    async def call() -> str:
        return "ok"

    assert await call() == "ok"
    assert cb.keyed("a").metrics().total_success_count == 1


async def test_keyed_sync_decorator() -> None:
    """A keyed circuit decorates a sync function."""
    cb = CircuitBreaker.consecutive_count("upstream")

    @cb.keyed("a")
    def call() -> str:
        return "ok"

    assert await asyncio.to_thread(call) == "ok"
    assert cb.keyed("a").metrics().total_success_count == 1


async def test_keyed_from_thread() -> None:
    """A keyed circuit admits and rejects from a worker thread."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=1)

    def call() -> None:
        with cb.keyed("a").from_thread:
            pass

    await asyncio.to_thread(call)

    assert cb.keyed("a").metrics().total_success_count == 1

    await cb.keyed("a").isolate()

    def denied() -> None:
        with cb.keyed("a").from_thread:
            pytest.fail("Expected not reached")

    with pytest.raises(CircuitBreakerError):
        await asyncio.to_thread(denied)


async def test_keyed_shares_logger_and_context_var() -> None:
    """Keyed circuits create no per-key logger or context variable."""
    cb = CircuitBreaker.consecutive_count("upstream")
    before = len(logging.Logger.manager.loggerDict)

    for index in range(KEYS):
        async with cb.keyed(f"tenant-{index}"):
            pass

    assert len(logging.Logger.manager.loggerDict) == before
    assert cb.keyed("tenant-0")._logger is cb._logger
    assert cb.keyed("tenant-0")._enter_stack is cb._enter_stack


async def test_keyed_circuits_are_not_tracked_for_reload() -> None:
    """Only the breaker is registered for external reload."""
    cb = CircuitBreaker.consecutive_count("upstream")
    cb.keyed("a")

    tracked = reconfigurable_instances()

    assert cb in tracked
    assert cb.keyed("a") not in tracked


async def test_evicts_least_recently_used_past_residency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Circuits past the residency window evict down to `maxsize`."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream", maxsize=MAXSIZE)

    for index in range(KEYS):
        async with cb.keyed(f"tenant-{index}"):
            pass

    assert len(cb._keyed) == KEYS

    clock = 1000.0
    async with cb.keyed("fresh"):
        pass

    assert len(cb._keyed) == MAXSIZE
    assert "fresh" in cb._keyed
    assert "tenant-0" not in cb._keyed


async def test_evicts_idle_open_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys that trip open once and go quiet do not pin the map."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count(
        "upstream", error_threshold=1, maxsize=1
    )

    for index in range(KEYS):
        clock += 1000.0
        await trip(cb.keyed(f"gone-{index}"), 1)

    assert len(cb._keyed) == 1


async def test_evicted_open_circuit_still_rejects() -> None:
    """The backend owns the state, so eviction cannot re-admit traffic."""
    cb = CircuitBreaker.consecutive_count(
        "upstream", error_threshold=1, maxsize=1
    )
    await trip(cb.keyed("bad"), 1)

    del cb._keyed["bad"]
    revived = cb.keyed("bad")

    assert revived.state == CircuitBreakerState.CLOSED
    with pytest.raises(CircuitBreakerError):
        async with revived:
            pytest.fail("Expected not reached")
    assert revived.state == CircuitBreakerState.OPEN


async def test_never_evicts_a_busy_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A circuit with a call in flight survives eviction pressure."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream", maxsize=1)

    async with cb.keyed("busy"):
        clock = 1000.0
        for index in range(10):
            async with cb.keyed(f"other-{index}"):
                pass
        assert "busy" in cb._keyed


async def test_maxsize_zero_keeps_every_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`maxsize=0` disables eviction."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream", maxsize=0)

    for index in range(KEYS):
        clock += 1000.0
        async with cb.keyed(f"tenant-{index}"):
            pass

    assert len(cb._keyed) == KEYS


async def test_reconfigure_propagates_to_keyed_circuits() -> None:
    """A breaker reconfigure republishes to every keyed circuit."""
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=5)
    async with cb.keyed("a"):
        pass

    await cb.reconfigure(ConsecutiveCountConfig(error_threshold=2))

    await trip(cb.keyed("a"), 2)

    assert cb.keyed("a").state == CircuitBreakerState.OPEN


async def test_keyed_circuit_cannot_be_keyed_again() -> None:
    """Keying a keyed circuit raises."""
    cb = CircuitBreaker.consecutive_count("upstream")

    with pytest.raises(ValueError, match="already keyed"):
        cb.keyed("a").keyed("b")


async def test_keyed_circuit_cannot_be_reconfigured() -> None:
    """Reconfiguring a keyed circuit raises."""
    cb = CircuitBreaker.consecutive_count("upstream")

    with pytest.raises(ValueError, match="per-key circuit"):
        await cb.keyed("a").reconfigure(ConsecutiveCountConfig())


async def test_backend_reclaims_expired_circuits(
    backend: MemoryCircuitBreakerAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored state is bounded too, not just the local circuit map."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.memory.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=1)

    for index in range(KEYS):
        clock += 100_000.0
        await trip(cb.keyed(f"gone-{index}"), 1)

    assert len(backend._states) < KEYS


async def test_returning_key_starts_from_a_clean_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors nobody has touched for a lifetime do not count toward a trip."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.memory.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=ERRORS)
    await trip(cb.keyed("quiet"), ERRORS - 1)

    clock += DAY + 1
    await trip(cb.keyed("quiet"), 1)

    assert cb.keyed("quiet").state == CircuitBreakerState.CLOSED


async def test_backend_cull_is_bounded(
    backend: MemoryCircuitBreakerAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single write reclaims at most the cull limit."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.memory.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream", error_threshold=1)
    for index in range(cb_memory._CULL_LIMIT + UNCULLED):
        await trip(cb.keyed(f"old-{index}"), 1)

    clock += 100_000.0
    await trip(cb.keyed("new"), 1)

    # One write cannot sweep the whole keyspace.
    assert len(backend._states) > UNCULLED


async def test_forced_circuit_never_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator hold survives any quiet period."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.memory.monotonic", lambda: clock
    )
    cb = CircuitBreaker.consecutive_count("upstream")
    await cb.keyed("held").isolate()

    clock += 100_000_000.0

    assert cb.keyed("held").state == CircuitBreakerState.FORCED_OPEN
    with pytest.raises(CircuitBreakerError):
        async with cb.keyed("held"):
            pytest.fail("Expected not reached")


async def test_cull_spares_another_breaker_still_cooling_down(
    backend: MemoryCircuitBreakerAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One breaker's traffic cannot reclaim another's live circuit."""
    clock = 0.0
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.monotonic", lambda: clock
    )
    monkeypatch.setattr(
        "grelmicro.resilience.circuitbreaker.memory.monotonic", lambda: clock
    )
    slow = CircuitBreaker.consecutive_count(
        "slow", error_threshold=1, reset_timeout=LONG_COOL_DOWN
    )
    fast = CircuitBreaker.consecutive_count("fast", error_threshold=1)
    await trip(slow, 1)

    # A day of quiet: past the flat lifetime, inside 10x slow's cool-down.
    clock += DAY + 1

    for index in range(3):
        await trip(fast.keyed(f"k{index}"), 1)

    assert "slow" in backend._states
    assert slow.state == CircuitBreakerState.OPEN
