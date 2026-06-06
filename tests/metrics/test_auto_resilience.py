"""Auto-instrumentation tests for the resilience components."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    RateLimiter,
    Retry,
    Timeout,
)
from grelmicro.resilience.circuitbreaker import CircuitBreakerState
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.errors import (
    BulkheadFullError,
    CircuitBreakerError,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

if TYPE_CHECKING:
    from tests.metrics.conftest import MetricsHarness


async def test_retry_emits_success(metrics_reader: MetricsHarness) -> None:
    """A run that succeeds emits attempts(outcome=success) and duration."""
    policy = Retry("flaky", when=ValueError, attempts=3)

    @policy
    async def ok() -> str:
        return "ok"

    assert await ok() == "ok"
    attempts = metrics_reader.points("grelmicro.retry.attempts")
    assert attempts[0][1] == {"retry.name": "flaky", "outcome": "success"}
    assert metrics_reader.points("grelmicro.retry.duration")[0][1] == {
        "retry.name": "flaky"
    }


async def test_retry_emits_error_on_exhaustion(
    metrics_reader: MetricsHarness,
) -> None:
    """Exhausting retries emits attempts(outcome=error)."""
    policy = Retry("always", when=ValueError, attempts=2, backoff=_no_backoff())

    @policy
    async def boom() -> None:
        raise ValueError

    with pytest.raises(ValueError):  # noqa: PT011
        await boom()
    attempts = metrics_reader.points("grelmicro.retry.attempts")
    assert attempts[0][1]["outcome"] == "error"


def test_retry_sync_emits(metrics_reader: MetricsHarness) -> None:
    """The sync run path emits the same metrics."""
    policy = Retry("sync", when=ValueError, attempts=2)

    @policy
    def ok() -> int:
        return 1

    assert ok() == 1
    assert (
        metrics_reader.points("grelmicro.retry.attempts")[0][1]["outcome"]
        == "success"
    )


async def test_circuit_breaker_emits_calls_and_state(
    metrics_reader: MetricsHarness,
) -> None:
    """A success call emits calls(result=success) and a state gauge."""
    cb = CircuitBreaker.consecutive_count(
        "payments", error_threshold=2, backend=MemoryCircuitBreakerAdapter()
    )
    async with cb:
        pass
    calls = metrics_reader.points("grelmicro.circuit_breaker.calls")
    assert calls[0][1] == {
        "circuit_breaker.name": "payments",
        "result": "success",
    }
    state = metrics_reader.points("grelmicro.circuit_breaker.state")
    assert state[0][1] == {"circuit_breaker.name": "payments"}


async def test_circuit_breaker_emits_transition_and_rejected(
    metrics_reader: MetricsHarness,
) -> None:
    """Tripping the breaker emits a transition and a rejected call."""
    cb = CircuitBreaker.consecutive_count(
        "api", error_threshold=1, backend=MemoryCircuitBreakerAdapter()
    )
    with pytest.raises(RuntimeError):
        async with cb:
            raise RuntimeError
    transitions = metrics_reader.points("grelmicro.circuit_breaker.transitions")
    assert transitions[0][1]["from"] == str(CircuitBreakerState.CLOSED)
    assert transitions[0][1]["to"] == str(CircuitBreakerState.OPEN)

    with pytest.raises(CircuitBreakerError):
        async with cb:
            pass
    calls = metrics_reader.points("grelmicro.circuit_breaker.calls")
    assert any(attrs["result"] == "rejected" for _, attrs in calls)


async def test_rate_limiter_emits_decisions(
    metrics_reader: MetricsHarness,
) -> None:
    """Each acquire emits a decision (allowed or limited)."""
    rl = RateLimiter.sliding_window(
        "api", limit=1, window=60, backend=MemoryRateLimiterAdapter()
    )
    await rl.acquire(key="user-1")
    await rl.acquire(key="user-1")
    decisions = metrics_reader.points("grelmicro.rate_limiter.decisions")
    seen = {attrs["decision"] for _, attrs in decisions}
    assert "allowed" in seen
    assert all(attrs["rate_limiter.name"] == "api" for _, attrs in decisions)


async def test_bulkhead_emits_active_and_rejections(
    metrics_reader: MetricsHarness,
) -> None:
    """Admission moves the active gauge and a full bulkhead emits rejections."""
    bulkhead = Bulkhead("db", max_concurrent=1, max_wait=0)
    async with bulkhead:
        # A nested acquire on the only permit is rejected immediately.
        with pytest.raises(BulkheadFullError):
            async with bulkhead:
                pass
    active = metrics_reader.points("grelmicro.bulkhead.active")
    assert active[0][0] == 0  # net zero after exit
    rejections = metrics_reader.points("grelmicro.bulkhead.rejections")
    assert rejections[0][1] == {"bulkhead.name": "db"}


async def test_timeout_emits_exceeded(
    metrics_reader: MetricsHarness,
) -> None:
    """A blown deadline emits the exceeded counter."""
    import asyncio  # noqa: PLC0415

    timeout = Timeout("slow", seconds=0.01)
    with pytest.raises(TimeoutError):
        async with timeout:
            await asyncio.sleep(0.2)
    exceeded = metrics_reader.points("grelmicro.timeout.exceeded")
    assert exceeded[0][1] == {"timeout.name": "slow"}


async def test_timeout_no_emit_when_within_deadline(
    metrics_reader: MetricsHarness,
) -> None:
    """A call within the deadline does not emit the exceeded counter."""
    timeout = Timeout("fast", seconds=5)
    async with timeout:
        pass
    assert metrics_reader.points("grelmicro.timeout.exceeded") == []


def _no_backoff() -> object:
    """Build a near-zero-delay backoff for fast retry tests."""
    from grelmicro.resilience import ConstantBackoff  # noqa: PLC0415

    return ConstantBackoff(delay=0.001)
