"""Tests for CircuitBreaker construction paths."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from grelmicro._config import reconfigure_all
from grelmicro.errors import SettingsValidationError
from grelmicro.resilience import Match, Outcome
from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    ConsecutiveCountConfig,
)

ERROR_KWARG = 7
DEFAULT_ERROR = 5
DEFAULT_SUCCESS = 2
DEFAULT_RESET = timedelta(seconds=30)
DEFAULT_HALF_OPEN_CAPACITY = 1
DEFAULT_LOG_LEVEL = "WARNING"

_FACTORY_SUCCESS = 3
_FACTORY_RESET = timedelta(seconds=15)
_FACTORY_HALF_OPEN = 2


def test_bare_constructor_uses_consecutive_count_defaults() -> None:
    """`CircuitBreaker("name")` builds with `ConsecutiveCountConfig()` defaults."""
    cb = CircuitBreaker("payments")
    assert cb.name == "payments"
    assert cb.config.error_threshold == DEFAULT_ERROR
    assert cb.config.success_threshold == DEFAULT_SUCCESS
    assert cb.config.reset_timeout == DEFAULT_RESET
    assert cb.config.half_open_capacity == DEFAULT_HALF_OPEN_CAPACITY
    assert cb.config.log_level == DEFAULT_LOG_LEVEL


def test_from_config_uses_given_config() -> None:
    """`CircuitBreaker.from_config()` constructs from a name and a config."""
    cfg = ConsecutiveCountConfig(
        error_threshold=ERROR_KWARG, reset_timeout=timedelta(seconds=10)
    )
    cb = CircuitBreaker.from_config("payments", cfg)
    assert cb.name == "payments"
    assert cb.config is cfg


def test_consecutive_count_factory_with_no_kwargs_uses_defaults() -> None:
    """`CircuitBreaker.consecutive_count(name)` builds with all defaults."""
    cb = CircuitBreaker.consecutive_count("payments")
    assert cb.config.error_threshold == DEFAULT_ERROR
    assert cb.config.success_threshold == DEFAULT_SUCCESS
    assert cb.config.reset_timeout == DEFAULT_RESET
    assert cb.config.half_open_capacity == DEFAULT_HALF_OPEN_CAPACITY
    assert cb.config.log_level == DEFAULT_LOG_LEVEL
    assert cb.config.when(Outcome.from_exception(LookupError()))


def test_consecutive_count_factory_with_every_kwarg() -> None:
    """The factory forwards every kwarg into the built `ConsecutiveCountConfig`."""
    cb = CircuitBreaker.consecutive_count(
        "payments",
        when=(ValueError,),
        error_threshold=ERROR_KWARG,
        success_threshold=_FACTORY_SUCCESS,
        reset_timeout=_FACTORY_RESET,
        half_open_capacity=_FACTORY_HALF_OPEN,
        log_level="DEBUG",
    )
    assert cb.config.error_threshold == ERROR_KWARG
    assert cb.config.success_threshold == _FACTORY_SUCCESS
    assert cb.config.reset_timeout == _FACTORY_RESET
    assert cb.config.half_open_capacity == _FACTORY_HALF_OPEN
    assert cb.config.log_level == "DEBUG"
    assert cb.config.when(Outcome.from_exception(ValueError()))


def test_consecutive_count_config_default_when_matches_every_exception() -> (
    None
):
    """The default `when` counts every `Exception` as a failure."""
    # Arrange
    config = ConsecutiveCountConfig()

    # Act
    matched = config.when(Outcome.from_exception(RuntimeError()))

    # Assert
    assert matched


@pytest.mark.parametrize(
    "when",
    [
        ValueError,
        (KeyError, ValueError),
        lambda error: isinstance(error, ValueError),
        Match.exception(ValueError),
        "builtins.ValueError",
        "builtins.KeyError,builtins.ValueError",
        ("builtins.KeyError", ValueError),
    ],
    ids=[
        "class",
        "tuple",
        "predicate",
        "match",
        "fqn",
        "fqn-csv",
        "mixed-tuple",
    ],
)
def test_circuit_breaker_when_shorthand_matches_named_error(
    when: object,
) -> None:
    """Every `when=` shorthand `Retry` accepts names the failing errors."""
    # Arrange
    cb = CircuitBreaker.consecutive_count(
        "payments",
        when=when,  # ty: ignore[invalid-argument-type]
    )

    # Act
    named = cb.config.when(Outcome.from_exception(ValueError()))
    other = cb.config.when(Outcome.from_exception(RuntimeError()))

    # Assert
    assert named
    assert not other


def test_circuit_breaker_ignore_exceptions_keyword_raises_type_error() -> None:
    """The removed `ignore_exceptions=` keyword is refused."""
    # Act / Assert
    with pytest.raises(TypeError, match="ignore_exceptions"):
        CircuitBreaker.consecutive_count(
            "payments",
            ignore_exceptions=ValueError,  # type: ignore[call-arg]  # ty: ignore[unknown-argument]
        )


def test_consecutive_count_config_ignore_exceptions_field_is_refused() -> None:
    """The removed `ignore_exceptions` field is refused by the config."""
    # Act / Assert
    with pytest.raises(ValidationError, match="ignore_exceptions"):
        ConsecutiveCountConfig(ignore_exceptions=(ValueError,))  # type: ignore[call-arg]  # ty: ignore[unknown-argument]


def test_circuit_breaker_when_from_environment_names_failing_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_CIRCUITBREAKER_{NAME}_WHEN` reads fully-qualified class names."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(
        "GREL_CIRCUITBREAKER_PAYMENTS_WHEN",
        "builtins.KeyError,builtins.ValueError",
    )

    # Act
    cb = CircuitBreaker.consecutive_count("payments")

    # Assert
    assert cb.config.when(Outcome.from_exception(ValueError()))
    assert not cb.config.when(Outcome.from_exception(RuntimeError()))


def test_invalid_threshold_raises() -> None:
    """Non-positive threshold values raise `ValidationError`."""
    with pytest.raises(SettingsValidationError):
        CircuitBreaker.consecutive_count("payments", error_threshold=0)


async def test_circuit_breaker_reconfigure_with_identical_config_keeps_binding() -> (
    None
):
    """Reconfiguring with an identical config keeps the bound strategy."""
    # Arrange
    cb = CircuitBreaker.from_config(
        "same-config", ConsecutiveCountConfig(when=ValueError)
    )
    state = cb._state

    # Act
    await cb.reconfigure(ConsecutiveCountConfig(when=ValueError))

    # Assert
    assert cb._state is state


async def test_circuit_breaker_resync_with_unchanged_when_keeps_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-reading the same `GREL_CIRCUITBREAKER_{NAME}_WHEN` text changes nothing."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_CIRCUITBREAKER_RESYNC_WHEN", "builtins.ValueError")
    cb = CircuitBreaker.consecutive_count("resync")
    state = cb._state

    # Act
    await reconfigure_all(
        {"GREL_CIRCUITBREAKER_RESYNC_WHEN": "builtins.ValueError"}
    )

    # Assert
    assert cb._state is state
