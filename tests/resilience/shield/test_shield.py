"""Shield class and execution tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grelmicro.resilience import ApiShieldConfig, Shield
from grelmicro.resilience.errors import ResilienceError


class _SignalError(Exception):
    """Test-only retryable error used as a `timeout_errors` member."""


class _PermanentError(Exception):
    """Test-only non-retryable error."""


def _shield(**overrides: Any) -> Shield:  # noqa: ANN401
    """Build an api-profile Shield with deterministic test sources."""
    return Shield.api(
        "test",
        timeout_errors=(_SignalError,),
        **overrides,
    )


async def test_success_path_no_retry_refunds_one() -> None:
    """Success without a retry refunds 1 token, clamped at capacity."""
    s = _shield()
    starting = s._state.retry_budget.available
    # Drain one slot first so a refund has somewhere to go.
    await s._state.retry_budget.try_acquire()
    assert s._state.retry_budget.available == starting - 1

    async def ok() -> str:
        return "ok"

    assert await s.run(ok) == "ok"
    # Refund of 1 happened, bringing us back to starting.
    assert s._state.retry_budget.available == starting


async def test_single_retry_then_success_net_zero_budget() -> None:
    """One retry consumed and recovered yields net zero budget change."""
    s = _shield()
    capacity = s._state.retry_budget.capacity

    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _SignalError
        return "done"

    assert await s.run(flaky) == "done"
    assert attempts["count"] == 2  # noqa: PLR2004
    assert s._state.retry_budget.available == capacity


async def test_attempts_exhausted_attaches_pep_678_note() -> None:
    """All four attempts fail. The final exception carries a `shield:` note."""
    s = _shield()

    async def always_fails() -> None:
        raise _SignalError

    with pytest.raises(_SignalError) as exc_info:
        await s.run(always_fails)
    notes = exc_info.value.__notes__
    assert any("shield: " in note for note in notes)
    assert any(
        "attempts exhausted" in note or "budget" in note for note in notes
    )
    assert any("api profile" in note for note in notes)


async def test_budget_exhausted_stops_loop_silently() -> None:
    """An empty budget surfaces the failure with the budget note."""
    s = _shield()
    # Drain the budget by acquiring every token directly.
    while await s._state.retry_budget.try_acquire():
        pass

    async def always_fails() -> None:
        raise _SignalError

    with pytest.raises(_SignalError) as exc_info:
        await s.run(always_fails)
    notes = exc_info.value.__notes__
    assert any("budget exhausted" in note for note in notes)


async def test_non_timeout_exception_propagates_without_retry() -> None:
    """Non-`timeout_errors` Exception propagates immediately."""
    s = _shield()
    attempts = {"count": 0}

    async def boom() -> None:
        attempts["count"] += 1
        raise _PermanentError

    with pytest.raises(_PermanentError):
        await s.run(boom)
    assert attempts["count"] == 1


async def test_resilience_error_propagates_without_retry() -> None:
    """`ResilienceError` subclasses propagate immediately."""

    class _BlowUpError(ResilienceError):
        pass

    s = _shield()
    attempts = {"count": 0}
    blowup_msg = "nope"

    async def boom() -> None:
        attempts["count"] += 1
        raise _BlowUpError(blowup_msg)

    with pytest.raises(_BlowUpError):
        await s.run(boom)
    assert attempts["count"] == 1


async def test_resilience_error_skips_cache_and_fallback() -> None:
    """`ResilienceError` bypasses every recovery path and carries no note."""

    class _BlowUpError(ResilienceError):
        pass

    class _StubCache:
        async def get(self, _key: str) -> str:
            return "should-not-be-returned"

        async def set(self, _key: str, _value: str) -> None:
            return None

    async def synth(_exc: BaseException) -> str:
        return "fallback-value"

    s = Shield.api(
        "rescue",
        timeout_errors=(TimeoutError,),
        cache=_StubCache(),
        fallback=synth,
    )

    boom_msg = "nope"

    async def boom() -> None:
        raise _BlowUpError(boom_msg)

    with pytest.raises(_BlowUpError) as exc_info:
        await s.run(boom)
    assert not getattr(exc_info.value, "__notes__", [])


async def test_base_exception_propagates_without_retry() -> None:
    """`BaseException` outside `Exception` is never retried."""
    s = _shield()
    attempts = {"count": 0}

    async def boom() -> None:
        attempts["count"] += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await s.run(boom)
    assert attempts["count"] == 1


async def test_timeout_errors_tuple_merged_with_timeout_error() -> None:
    """Both user types and `TimeoutError` are retryable."""
    s = Shield.api("merged", timeout_errors=(_SignalError,))
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError
        return "ok"

    assert await s.run(flaky) == "ok"
    assert attempts["count"] == 2  # noqa: PLR2004


async def test_decorator_form_works() -> None:
    """The `@shield_instance` decorator wraps async functions."""
    s = _shield()

    @s
    async def call() -> str:
        return "x"

    assert await call() == "x"


async def test_decorator_rejects_sync_functions() -> None:
    """Sync functions cannot be wrapped by Shield."""
    s = _shield()
    with pytest.raises(TypeError, match="async"):
        s(lambda: None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_run_rejects_sync_functions() -> None:
    """`Shield.run` raises a clear error for sync callables."""
    s = _shield()
    with pytest.raises(TypeError, match="async"):
        await s.run(lambda: None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_state_shared_across_calls_through_one_instance() -> None:
    """Two functions wrapped by one Shield share the same budget."""
    s = _shield()
    capacity = s._state.retry_budget.capacity

    @s
    async def fail() -> None:
        raise _SignalError

    with pytest.raises(_SignalError):
        await fail()
    # Three retries got consumed on the failed call (no recovered retries).
    assert s._state.retry_budget.available == capacity - 3


async def test_from_config_constructs_instance() -> None:
    """`Shield.from_config` accepts a pre-built profile config."""
    config = ApiShieldConfig(timeout_errors=(_SignalError,))
    s = Shield.from_config("by_config", config)
    assert s.name == "by_config"
    assert s.config is config


async def test_name_is_required_on_factory() -> None:
    """`Shield.api(name)` requires the name positionally."""
    with pytest.raises(TypeError):
        Shield.api()  # type: ignore[call-arg]  # ty: ignore[missing-argument]


async def test_pep_678_note_format() -> None:
    """The note follows the documented format."""
    s = _shield()

    async def always_fails() -> None:
        raise _SignalError

    with pytest.raises(_SignalError) as exc_info:
        await s.run(always_fails)
    notes = exc_info.value.__notes__
    note = next(n for n in notes if n.startswith("shield: "))
    # Format: shield: <reason> after <n>/4 attempts in <e>s (api profile)
    assert "after" in note
    assert "/4 attempts" in note
    assert "(api profile)" in note


async def test_run_accepts_functools_partial() -> None:
    """`functools.partial` of an async function is recognised."""
    import functools  # noqa: PLC0415

    s = _shield()

    async def add(x: int, y: int) -> int:
        return x + y

    bound = functools.partial(add, 1)
    assert await s.run(bound, 2) == 3  # noqa: PLR2004


async def test_reconfigure_swaps_state() -> None:
    """`reconfigure` publishes a fresh config and rebuilds derived state."""
    s = _shield()
    new = ApiShieldConfig(timeout_errors=(_SignalError,), max_rate=42.0)
    await s.reconfigure(new)
    assert s.config.max_rate == 42.0  # noqa: PLR2004
