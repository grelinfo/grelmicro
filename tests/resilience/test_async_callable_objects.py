"""Every decorator treats an async callable object as async.

`inspect.iscoroutinefunction` reads `False` for an object whose
`__call__` is async, so the decorators built the sync wrapper for it.
The wrapper called the object, took the coroutine it returned as the
result, and returned. The body ran later, when the caller awaited that
coroutine, outside the policy: `@retry` never retried, and `@timeout`
and `@bulkhead` refused a callable that was async all along.

`grelmicro._async.is_async_callable` is the check that reads it right.
"""

from __future__ import annotations

import pytest

from grelmicro.cache import cached
from grelmicro.metrics import measure
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    ConstantBackoff,
    Retry,
    Timeout,
    shield,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.trace import instrument

ATTEMPTS = 3


class _Flaky:
    """Callable object whose async `__call__` always raises."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        msg = "boom"
        raise ValueError(msg)


class _Sync:
    """Callable object whose `__call__` is not async."""

    def __call__(self) -> str:
        return "sync"


async def test_retry_retries_an_async_callable_object() -> None:
    """The policy engaged on the object, rather than reporting it had.

    This is the one that cost correctness rather than observability: the
    retry counted a coroutine as a successful result and returned it, so
    the body ran once and the caller saw the first failure.
    """
    flaky = _Flaky()
    policy = Retry(
        "orders",
        when=ValueError,
        attempts=ATTEMPTS,
        backoff=ConstantBackoff(delay=0.001),
    )

    with pytest.raises(ValueError, match="boom"):
        await policy(flaky)()

    assert flaky.calls == ATTEMPTS


async def test_a_circuit_breaker_admits_an_async_callable_object() -> None:
    """The breaker runs it on the event loop, not through the thread door."""
    flaky = _Flaky()
    breaker = CircuitBreaker.consecutive_count(
        "upstream", error_threshold=5, backend=MemoryCircuitBreakerAdapter()
    )

    with pytest.raises(ValueError, match="boom"):
        await breaker(flaky)()

    assert flaky.calls == 1


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: Timeout("slow", seconds=1), id="timeout"),
        pytest.param(lambda: Bulkhead("db", max_concurrent=1), id="bulkhead"),
        pytest.param(lambda: shield, id="shield"),
        pytest.param(lambda: measure, id="measure"),
        pytest.param(lambda: instrument, id="instrument"),
        pytest.param(lambda: cached(ttl=30, key="k"), id="cached"),
    ],
)
async def test_an_async_callable_object_is_accepted(build: object) -> None:
    """None of them refuses a callable that is async through `__call__`."""
    flaky = _Flaky()

    wrapped = build()(flaky)  # type: ignore[operator]  # ty: ignore[call-non-callable]
    with pytest.raises(ValueError, match="boom"):
        await wrapped()

    assert flaky.calls == 1


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: Timeout("slow", seconds=1), id="timeout"),
        pytest.param(lambda: Bulkhead("db", max_concurrent=1), id="bulkhead"),
        pytest.param(lambda: shield, id="shield"),
        pytest.param(lambda: cached(ttl=30, key="k"), id="cached"),
    ],
)
def test_a_sync_callable_object_is_still_refused(build: object) -> None:
    """The refusals that were right stay right.

    An object whose `__call__` is not async is sync code, and each of
    these refuses sync code for a reason of its own.
    """
    with pytest.raises(TypeError):
        build()(_Sync())  # type: ignore[operator]  # ty: ignore[call-non-callable]
