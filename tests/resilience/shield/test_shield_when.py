"""Shield `when=` outcome filter tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grelmicro._config import reconfigure_all
from grelmicro.errors import SettingsValidationError
from grelmicro.resilience import ApiShieldConfig, Match, Shield


class _SignalError(Exception):
    """Test-only error that `when=` names."""


class _PermanentError(Exception):
    """Test-only error that `when=` does not name."""


@pytest.mark.parametrize(
    "build",
    [
        lambda: Shield("bare"),
        lambda: Shield("bare", env_load=False),
        lambda: Shield.api("bare"),
        lambda: Shield.internal("bare"),
        lambda: Shield.slow("bare"),
    ],
    ids=["constructor", "constructor-env-off", "api", "internal", "slow"],
)
def test_shield_without_when_raises_settings_error_naming_when(
    build: Any,  # noqa: ANN401
) -> None:
    """Every Shield door refuses a missing `when=` and names it."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="when"):
        build()


def test_shield_timeout_errors_keyword_raises_type_error() -> None:
    """The removed `timeout_errors=` keyword is refused."""
    # Act / Assert
    with pytest.raises(TypeError, match="timeout_errors"):
        Shield.api(
            "old",
            timeout_errors=(_SignalError,),  # type: ignore[call-arg]  # ty: ignore[unknown-argument]
        )


@pytest.mark.parametrize(
    "when",
    [
        _SignalError,
        (KeyError, _SignalError),
        lambda error: isinstance(error, _SignalError),
        Match.exception(_SignalError),
    ],
    ids=["class", "tuple", "predicate", "match"],
)
async def test_shield_when_shorthand_retries_named_error(
    when: Any,  # noqa: ANN401
) -> None:
    """Every `when=` shorthand `Retry` accepts names the retried errors."""
    # Arrange
    s = Shield.api("shorthand", when=when)
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise _SignalError
        return "ok"

    # Act
    result = await s.run(flaky)

    # Assert
    assert result == "ok"
    assert attempts["count"] == 2  # noqa: PLR2004


async def test_shield_unmatched_error_goes_to_fallback_without_retry() -> None:
    """An error `when=` does not match skips retries and reaches recovery."""
    # Arrange
    attempts = {"count": 0}

    async def recover(_error: BaseException) -> str:
        return "recovered"

    s = Shield.api("recovery", when=_SignalError, fallback=recover)

    async def fail() -> str:
        attempts["count"] += 1
        raise _PermanentError

    # Act
    result = await s.run(fail)

    # Assert
    assert result == "recovered"
    assert attempts["count"] == 1


async def test_shield_own_timeout_retried_even_if_when_leaves_it_out() -> None:
    """An attempt that outlives Shield's own timeout is retried anyway."""
    # Arrange
    s = Shield.internal("own-timeout", when=_SignalError)
    attempts = {"count": 0}

    async def slow_once() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            await asyncio.Event().wait()
        return "ok"

    # Act
    result = await s.run(slow_once)

    # Assert
    assert result == "ok"
    assert attempts["count"] == 2  # noqa: PLR2004


async def test_shield_reconfigure_with_identical_config_keeps_state() -> None:
    """Reconfiguring with an identical config keeps the rate gate and budget."""
    # Arrange
    s = Shield.api("same-config", when=_SignalError)
    state = s._state

    # Act
    await s.reconfigure(ApiShieldConfig(when=_SignalError))

    # Assert
    assert s._state is state


async def test_shield_resync_with_unchanged_when_keeps_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-reading the same `GREL_SHIELD_{NAME}_WHEN` text changes nothing."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_SHIELD_RESYNC_WHEN", "builtins.ValueError")
    s = Shield.api("resync")
    state = s._state

    # Act
    await reconfigure_all({"GREL_SHIELD_RESYNC_WHEN": "builtins.ValueError"})

    # Assert
    assert s._state is state
