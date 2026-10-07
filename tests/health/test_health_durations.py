"""Health `cache_ttl` and `timeout` settings.

`cache_ttl` takes whole seconds or a `timedelta`, zero for off, and the
result cache is checked at its exact boundary on a clock the test moves,
in whole nanoseconds. A `timeout` is a finite number of seconds above zero,
on the config and on each check.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Self

import pytest
from hypothesis import given
from pydantic import ValidationError

from grelmicro._duration import MAX_DURATION, MICROSECOND
from grelmicro.errors import SettingsValidationError
from grelmicro.health import _checks as checks_module
from grelmicro.health._checks import HealthChecks, HealthChecksConfig
from grelmicro.providers._base import Provider
from tests._durations import DURATIONS, FLOATS, NANOSECOND, NanosecondClock

if TYPE_CHECKING:
    from collections.abc import Callable

WHOLE = 60
UNDER_A_SECOND = timedelta(milliseconds=500)
TWO_ROUNDS = 2

READ_BACK = [
    pytest.param(WHOLE, timedelta(seconds=WHOLE), id="int"),
    pytest.param(UNDER_A_SECOND, UNDER_A_SECOND, id="timedelta"),
    pytest.param(0, timedelta(0), id="zero"),
]

FROM_TEXT = [
    pytest.param("60", timedelta(seconds=WHOLE), id="seconds"),
    pytest.param("PT0.5S", UNDER_A_SECOND, id="iso-8601"),
    pytest.param("0", timedelta(0), id="zero-seconds"),
    pytest.param("PT0S", timedelta(0), id="zero-iso-8601"),
]


class Counting:
    """A health check that counts its runs."""

    def __init__(self) -> None:
        """Start with no runs."""
        self.calls = 0

    async def __call__(self) -> None:
        """Count one run."""
        self.calls += 1


async def _runs_after(cache_ttl: timedelta, extra_ns: int) -> int:
    """Return the check runs of two rounds `cache_ttl` plus `extra_ns` apart."""
    clock = NanosecondClock()
    check = Counting()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(checks_module, "monotonic_ns", clock)
        health = HealthChecks(cache_ttl=cache_ttl, env_load=False)
        health.add("db", check)
        await health.run()
        clock.advance(cache_ttl, extra_ns)
        await health.run()
    return check.calls


# --- Public API ---


@pytest.mark.parametrize("value", FLOATS)
def test_health_checks_config_float_cache_ttl_refused(value: object) -> None:
    """A float or a bool `cache_ttl` is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="cache_ttl must be whole seconds or a timedelta"
    ):
        HealthChecksConfig(cache_ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", [-1, timedelta(microseconds=-1)])
def test_health_checks_config_negative_cache_ttl_refused(
    value: int | timedelta,
) -> None:
    """A negative `cache_ttl` is refused when the config is built."""
    # Act / Assert
    with pytest.raises(ValidationError, match="cache_ttl must not be negative"):
        HealthChecksConfig(cache_ttl=value)


def test_health_checks_config_cache_ttl_over_a_hundred_years_refused() -> None:
    """A `cache_ttl` over a hundred years is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="cache_ttl must be at most 100 years"
    ):
        HealthChecksConfig(cache_ttl=MAX_DURATION + MICROSECOND)


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_health_checks_config_cache_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds, zero and a `timedelta` read back as a `timedelta`."""
    # Act
    config = HealthChecksConfig(cache_ttl=value)

    # Assert
    assert config.cache_ttl == expected


def test_health_checks_config_cache_ttl_defaults_to_one_second() -> None:
    """A check result is cached for one second when unsaid."""
    # Act
    config = HealthChecksConfig()

    # Assert
    assert config.cache_ttl == timedelta(seconds=1)


@pytest.mark.parametrize(
    ("value", "text"),
    [(timedelta(milliseconds=1500), "PT1.5S"), (timedelta(0), "PT0S")],
)
def test_health_checks_config_dumps_cache_ttl_as_iso_8601(
    value: timedelta, text: str
) -> None:
    """A `cache_ttl` dumps to JSON as ISO 8601 and reads back exactly."""
    # Arrange
    config = HealthChecksConfig(cache_ttl=value)

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["cache_ttl"] == text
    assert HealthChecksConfig.model_validate(dumped).cache_ttl == value


@pytest.mark.parametrize("value", FLOATS)
def test_health_checks_float_cache_ttl_refused(value: Any) -> None:  # noqa: ANN401
    """A float or a bool `cache_ttl` keyword is refused, naming the setting."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="cache_ttl must be whole seconds or a timedelta",
    ):
        HealthChecks(cache_ttl=value, env_load=False)


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_health_checks_cache_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """A `cache_ttl` keyword in whole seconds or a `timedelta` reads back."""
    # Act
    health = HealthChecks(cache_ttl=value, env_load=False)

    # Assert
    assert health.config.cache_ttl == expected


@pytest.mark.parametrize(("raw", "expected"), FROM_TEXT)
def test_health_checks_cache_ttl_from_environment_reads_as_timedelta(
    raw: str, expected: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is whole seconds or an ISO 8601 duration, zero included."""
    # Arrange
    monkeypatch.setenv("GREL_HEALTH_CACHE_TTL", raw)

    # Act
    health = HealthChecks(env_load=True)

    # Assert
    assert health.config.cache_ttl == expected


def test_health_checks_decimal_cache_ttl_from_environment_refused_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds is refused, and the error never repeats it."""
    # Arrange
    monkeypatch.setenv("GREL_HEALTH_CACHE_TTL", "4321.5")

    # Act
    with pytest.raises(SettingsValidationError) as refused:
        HealthChecks(env_load=True)

    # Assert
    assert "4321.5" not in str(refused.value)


# --- Result cache ---


@given(cache_ttl=DURATIONS)
def test_health_checks_result_inside_cache_ttl_served_from_cache(
    cache_ttl: timedelta,
) -> None:
    """A result one nanosecond short of `cache_ttl` old is served again."""
    # Act
    calls = asyncio.run(_runs_after(cache_ttl, -NANOSECOND))

    # Assert
    assert calls == 1


@given(cache_ttl=DURATIONS)
def test_health_checks_result_at_cache_ttl_runs_check_again(
    cache_ttl: timedelta,
) -> None:
    """A result `cache_ttl` old is never served, to the nanosecond."""
    # Act
    calls = asyncio.run(_runs_after(cache_ttl, 0))

    # Assert
    assert calls == TWO_ROUNDS


@pytest.mark.parametrize("raw", ["0", "PT0S"])
async def test_health_checks_zero_cache_ttl_from_environment_caches_nothing(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text `"0"` or `"PT0S"` runs the check on every round."""
    # Arrange
    monkeypatch.setenv("GREL_HEALTH_CACHE_TTL", raw)
    health = HealthChecks(env_load=True)
    check = Counting()
    health.add("db", check)

    # Act
    await health.run()
    await health.run()

    # Assert
    assert check.calls == TWO_ROUNDS


async def test_health_checks_zero_cache_ttl_caches_nothing_on_a_still_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cache_ttl=0` runs the check again even when no time has passed."""
    # Arrange
    monkeypatch.setattr(checks_module, "monotonic_ns", NanosecondClock())
    health = HealthChecks(cache_ttl=0, env_load=False)
    check = Counting()
    health.add("db", check)

    # Act
    await health.run()
    await health.run()

    # Assert
    assert check.calls == TWO_ROUNDS


# --- Timeout ---

NON_FINITE = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="minus-inf"),
]

NOT_POSITIVE = [
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param(-0.5, id="negative-float"),
]

NOT_A_NUMBER = [
    pytest.param("5", id="text"),
    pytest.param(True, id="bool"),
    pytest.param([5], id="list"),
]


class ReadyProvider(Provider):
    """A provider with a readiness check that always passes."""

    short_name = "ready"

    async def check(self) -> None:
        """Pass."""

    async def __aenter__(self) -> Self:
        """Open the provider (no-op)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the provider (no-op)."""


def _add(health: HealthChecks, timeout: Any) -> None:  # noqa: ANN401
    health.add("db", Counting(), timeout=timeout)


def _decorate(health: HealthChecks, timeout: Any) -> None:  # noqa: ANN401
    health.check("db", timeout=timeout)(Counting())


def _add_provider(health: HealthChecks, timeout: Any) -> None:  # noqa: ANN401
    health.add_provider(ReadyProvider(), timeout=timeout)


REGISTER = [
    pytest.param(_add, id="add"),
    pytest.param(_decorate, id="check"),
    pytest.param(_add_provider, id="add-provider"),
]


@pytest.mark.parametrize("value", NON_FINITE)
def test_health_checks_config_non_finite_timeout_refused(value: float) -> None:
    """A `timeout` that is not a finite number is refused, naming the field."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="timeout must be a finite number"
    ):
        HealthChecksConfig(timeout=value)


def test_health_checks_config_bool_timeout_refused() -> None:
    """A bool `timeout` is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="timeout must be a number of seconds"
    ):
        HealthChecksConfig(timeout=True)


@pytest.mark.parametrize("value", NON_FINITE)
def test_health_checks_non_finite_timeout_refused(value: float) -> None:
    """A `timeout` keyword that is not a finite number is refused."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError, match="timeout must be a finite number"
    ):
        HealthChecks(timeout=value, env_load=False)


def test_health_checks_non_finite_timeout_from_environment_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env text `"inf"` for `timeout` is refused, naming the field."""
    # Arrange
    monkeypatch.setenv("GREL_HEALTH_TIMEOUT", "inf")

    # Act / Assert
    with pytest.raises(
        SettingsValidationError, match="timeout must be a finite number"
    ):
        HealthChecks(env_load=True)


@pytest.mark.parametrize("register", REGISTER)
@pytest.mark.parametrize("value", NON_FINITE)
def test_health_checks_per_check_non_finite_timeout_refused(
    register: Callable[[HealthChecks, Any], None], value: float
) -> None:
    """A per-check `timeout` that is not a finite number is refused."""
    # Arrange
    health = HealthChecks(env_load=False)

    # Act / Assert
    with pytest.raises(ValueError, match="timeout must be a finite number"):
        register(health, value)


@pytest.mark.parametrize("register", REGISTER)
@pytest.mark.parametrize("value", NOT_POSITIVE)
def test_health_checks_per_check_timeout_of_zero_or_less_refused(
    register: Callable[[HealthChecks, Any], None], value: float
) -> None:
    """A per-check `timeout` of zero or less is refused, as on the config."""
    # Arrange
    health = HealthChecks(env_load=False)

    # Act / Assert
    with pytest.raises(ValueError, match="timeout must be greater than zero"):
        register(health, value)


@pytest.mark.parametrize("register", REGISTER)
@pytest.mark.parametrize("value", NOT_A_NUMBER)
def test_health_checks_per_check_timeout_not_a_number_refused(
    register: Callable[[HealthChecks, Any], None], value: object
) -> None:
    """A per-check `timeout` that is not a number raises `ValueError`."""
    # Arrange
    health = HealthChecks(env_load=False)

    # Act / Assert
    with pytest.raises(ValueError, match="timeout must be a number of seconds"):
        register(health, value)


@pytest.mark.parametrize("register", REGISTER)
def test_health_checks_per_check_refused_timeout_registers_nothing(
    register: Callable[[HealthChecks, Any], None],
) -> None:
    """A check refused for its `timeout` is left out of the report."""
    # Arrange
    health = HealthChecks(env_load=False)

    # Act
    with pytest.raises(ValueError, match="timeout"):
        register(health, 0)

    # Assert
    assert asyncio.run(health.run())["checks"] == {}
