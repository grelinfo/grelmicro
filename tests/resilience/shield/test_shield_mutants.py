"""Exact-value Shield execution tests for boundary and formula survivors.

These pin behavior that loose assertions miss in the run loop: the
retry-budget refund equals the number of consumed retries, a sub-second
backoff still sleeps, recorded latency is the call duration, the cache
key depends on the call arguments, and a partial-wrapped coroutine is
accepted.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

import pytest

from grelmicro.resilience import ApiShieldConfig, Shield
from grelmicro.resilience.shield import _shield as shield_module

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

_PRIMED = 7
_PARTIAL_SUM = 15
_KWARG_SUM = 7
_THREE = 3
_CAP = 42.0


class _SignalError(Exception):
    """Test-only retryable error."""


class _Clock:
    """Manually advanced monotonic clock."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_two_retries_refund_matches_consumed() -> None:
    """Two consumed retries then success refunds exactly two tokens."""
    s = Shield(
        "refund-two",
        timeout_errors=(_SignalError,),
        random_source=lambda: 0.0,  # zero backoff, no real sleep
    )
    capacity = s._state.retry_budget.capacity
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < _THREE:
            raise _SignalError
        return "ok"

    assert await s.run(flaky) == "ok"
    assert attempts["count"] == _THREE
    # Two retries consumed two tokens, then a refund of exactly two
    # restores the budget. A refund of 1 would leave it one short.
    assert s._state.retry_budget.available == capacity


async def test_sub_second_backoff_still_sleeps(
    mocker: MockerFixture,
) -> None:
    """A backoff delay below one second still triggers a sleep."""
    delays: list[float] = []

    async def _spy(seconds: float) -> None:
        delays.append(seconds)

    mocker.patch.object(shield_module, "sleep", side_effect=_spy)
    # random 0.5 and a small scale keep the delay strictly between 0 and 1.
    s = Shield(
        "subsec-backoff",
        timeout_errors=(_SignalError,),
        random_source=lambda: 0.5,
    )
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _SignalError
        return "ok"

    assert await s.run(flaky) == "ok"
    assert len(delays) == 1
    assert 0.0 < delays[0] < 1.0


async def test_recorded_latency_is_call_duration() -> None:
    """The estimator records the call duration, not the sum of timestamps."""
    clock = _Clock(start=100.0)
    s = Shield(
        "latency",
        timeout_errors=(_SignalError,),
        time_source=clock,
        random_source=lambda: 0.0,
    )

    async def slow_ok() -> str:
        clock.advance(0.4)  # the call takes 0.4 virtual seconds
        return "ok"

    assert await s.run(slow_ok) == "ok"
    # The next estimate is p95 (single sample 0.4) times the 2.5 multiplier.
    # A `+` latency flip would record ~200.4 and blow past the clamp.
    assert s._state.timeout_estimator.estimate() == pytest.approx(0.4 * 2.5)


async def test_cache_key_depends_on_arguments() -> None:
    """The default cache key includes the call arguments."""
    store: dict[str, Any] = {}

    class _Cache:
        async def get(self, key: str) -> Any:  # noqa: ANN401
            return store.get(key)

        async def set(self, key: str, value: Any) -> None:  # noqa: ANN401
            store[key] = value

    s = Shield(
        "cache-key",
        timeout_errors=(_SignalError,),
        cache=_Cache(),
        random_source=lambda: 0.0,
    )

    async def echo(value: int) -> int:
        if value < 0:
            raise _SignalError
        return value

    # Prime the cache for value=7 via a success.
    assert await s.run(echo, _PRIMED) == _PRIMED
    # Let the fire-and-forget cache set complete.
    for task in list(s._pending_tasks):
        await task

    # A give-up for value=-1 must NOT return the 7 entry: the key differs.
    with pytest.raises(_SignalError):
        await s.run(echo, -1)


async def test_run_accepts_partial_coroutine() -> None:
    """`Shield.run` accepts a functools.partial wrapping a coroutine."""
    s = Shield("partial", timeout_errors=(_SignalError,))

    async def add(a: int, b: int) -> int:
        return a + b

    bound = functools.partial(add, 10)
    assert await s.run(bound, 5) == _PARTIAL_SUM


async def test_run_forwards_keyword_arguments() -> None:
    """`Shield.run` forwards keyword arguments to the wrapped call."""
    s = Shield("kwargs", timeout_errors=(_SignalError,))

    async def echo(a: int, *, b: int) -> int:
        return a + b

    assert await s.run(echo, 3, b=4) == _KWARG_SUM


async def test_custom_cache_key_receives_keyword_arguments() -> None:
    """A custom cache_key callable receives the call's keyword arguments."""
    seen: dict[str, Any] = {}

    def key_for(*args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        seen["args"] = args
        seen["kwargs"] = kwargs
        return "k"

    class _Cache:
        async def get(self, _key: str) -> Any:  # noqa: ANN401
            return None

        async def set(self, _key: str, _value: Any) -> None:  # noqa: ANN401
            return None

    s = Shield(
        "custom-key",
        timeout_errors=(_SignalError,),
        cache=_Cache(),
        cache_key=key_for,
        random_source=lambda: 0.0,
    )

    async def boom(*, flag: bool) -> None:
        del flag
        raise _SignalError

    with pytest.raises(_SignalError):
        await s.run(boom, flag=True)

    assert seen["kwargs"] == {"flag": True}


async def test_run_works_after_reconfigure() -> None:
    """Reconfigure rebuilds a usable state so the next call still runs."""
    s = Shield("reconf-run", timeout_errors=(_SignalError,))
    # A different config forces `_apply_reconfigure` to actually rebuild.
    await s.reconfigure(
        ApiShieldConfig(timeout_errors=(_SignalError,), max_rate=_CAP)
    )

    async def ok() -> str:
        return "ok"

    # A `_state = None` would raise AttributeError on the next call.
    assert await s.run(ok) == "ok"
    assert s._state.adaptive_gate._max_rate_cap == _CAP


def test_max_rate_caps_the_adaptive_gate() -> None:
    """A configured `max_rate` becomes the adaptive gate's rate ceiling."""
    s = Shield("capped", timeout_errors=(_SignalError,), max_rate=_CAP)

    assert s._state.adaptive_gate._max_rate_cap == _CAP


def test_no_max_rate_leaves_gate_uncapped() -> None:
    """Without `max_rate`, the gate cap falls back to the profile default."""
    s = Shield("uncapped", timeout_errors=(_SignalError,))

    # The api profile default cap is None (uncapped).
    assert s._state.adaptive_gate._max_rate_cap is None


async def test_zero_backoff_delay_does_not_sleep(
    mocker: MockerFixture,
) -> None:
    """A zero backoff delay skips the sleep entirely (`> 0`, not `>= 0`)."""
    slept: list[float] = []

    async def _spy(seconds: float) -> None:
        slept.append(seconds)

    mocker.patch.object(shield_module, "sleep", side_effect=_spy)
    s = Shield(
        "zero-backoff",
        timeout_errors=(_SignalError,),
        random_source=lambda: 0.0,  # delay = 0 * ceiling = 0
    )
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _SignalError
        return "ok"

    assert await s.run(flaky) == "ok"
    assert slept == []
