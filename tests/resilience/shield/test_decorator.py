"""Module-level `shield` decorator tests."""

from __future__ import annotations

import functools
from typing import Any

import pytest

from grelmicro.resilience import shield
from grelmicro.resilience.shield._shield import Shield


class _SignalError(Exception):
    """Test-only retryable error."""


async def test_zero_arg_decorator_uses_api_profile() -> None:
    """`@shield` (no parens) wraps with the `api` profile defaults."""

    @shield
    async def fn() -> str:
        return "ok"

    assert await fn() == "ok"


async def test_zero_arg_decorator_retries_timeout_error() -> None:
    """The default profile retries `TimeoutError`."""
    attempts = {"count": 0}

    @shield
    async def fn() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError
        return "ok"

    assert await fn() == "ok"
    assert attempts["count"] == 2  # noqa: PLR2004


async def test_api_factory_decorator() -> None:
    """`@shield.api(...)` uses the api profile."""

    @shield.api(timeout_errors=(_SignalError,))
    async def fn() -> str:
        return "api"

    assert await fn() == "api"


async def test_internal_factory_decorator() -> None:
    """`@shield.internal(...)` uses the internal profile."""

    @shield.internal(timeout_errors=(_SignalError,))
    async def fn() -> str:
        return "internal"

    assert await fn() == "internal"


async def test_slow_factory_decorator() -> None:
    """`@shield.slow(...)` uses the slow profile."""

    @shield.slow(timeout_errors=(_SignalError,))
    async def fn() -> str:
        return "slow"

    assert await fn() == "slow"


async def test_decorator_propagates_non_retryable() -> None:
    """Non-`timeout_errors` exceptions propagate without retry."""

    @shield.api(timeout_errors=(_SignalError,))
    async def fn() -> None:
        msg = "permanent"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="permanent"):
        await fn()


async def test_decorator_attaches_pep_678_note_on_give_up() -> None:
    """On give-up the decorator surfaces the exception with a `shield:` note."""

    @shield.api(timeout_errors=(_SignalError,))
    async def fn() -> None:
        raise _SignalError

    with pytest.raises(_SignalError) as exc_info:
        await fn()
    notes = exc_info.value.__notes__
    assert any("shield: " in note for note in notes)


async def test_named_decorator_keeps_user_name() -> None:
    """An explicit `name=` is preserved across calls."""

    @shield.api("my-service", timeout_errors=(_SignalError,))
    async def fn() -> None:
        raise _SignalError

    with pytest.raises(_SignalError) as exc_info:
        await fn()
    notes = exc_info.value.__notes__
    assert any("api profile" in note for note in notes)


def test_a_partial_is_named_after_the_function_it_wraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shield name is a metric attribute and an environment prefix.

    A `repr` of a `functools.partial` carries a memory address, so the
    name changed on every restart. That made the metric attribute
    unbounded and the `GREL_SHIELD_*` prefix impossible to bind, since
    an operator cannot write a variable whose name they cannot predict.
    """
    seen: list[str] = []
    original = Shield.api

    def spy(name: str, **kwargs: Any) -> Shield:  # noqa: ANN401
        seen.append(name)
        return original(name, **kwargs)

    monkeypatch.setattr(Shield, "api", spy)

    shield(functools.partial(_sample, "p"))

    assert seen == ["_sample"]


async def _sample(prefix: str, value: int) -> str:
    """Module-level sample for the shield naming test."""
    return f"{prefix}{value}"
