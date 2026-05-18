"""Tests for CircuitBreaker construction paths."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    ConsecutiveCountConfig,
)

ERROR_KWARG = 7
DEFAULT_ERROR = 5
DEFAULT_SUCCESS = 2
DEFAULT_RESET = 30.0
DEFAULT_HALF_OPEN_CAPACITY = 1
DEFAULT_LOG_LEVEL = "WARNING"

_FACTORY_SUCCESS = 3
_FACTORY_RESET = 15.0
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


def test_positional_config_uses_given_config() -> None:
    """`CircuitBreaker(name, config)` uses the passed config as-is."""
    cfg = ConsecutiveCountConfig(
        error_threshold=ERROR_KWARG, reset_timeout=10.0
    )
    cb = CircuitBreaker("payments", cfg)
    assert cb.config is cfg


def test_from_config_uses_given_config() -> None:
    """`CircuitBreaker.from_config()` constructs from a name and a config."""
    cfg = ConsecutiveCountConfig(
        error_threshold=ERROR_KWARG, reset_timeout=10.0
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
    assert cb.config.ignore_exceptions == ()


def test_consecutive_count_factory_with_every_kwarg() -> None:
    """The factory forwards every kwarg into the built `ConsecutiveCountConfig`."""
    cb = CircuitBreaker.consecutive_count(
        "payments",
        ignore_exceptions=(ValueError,),
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
    assert cb.config.ignore_exceptions == (ValueError,)


def test_ignore_exceptions_accepts_single_class() -> None:
    """`ignore_exceptions=SomeError` is accepted as shorthand for `(SomeError,)`."""
    cb = CircuitBreaker.consecutive_count(
        "payments", ignore_exceptions=ValueError
    )
    assert cb.config.ignore_exceptions == (ValueError,)


def test_ignore_exceptions_accepts_tuple() -> None:
    """`ignore_exceptions=(A, B)` is preserved."""
    cb = CircuitBreaker.consecutive_count(
        "payments", ignore_exceptions=(ValueError, RuntimeError)
    )
    assert cb.config.ignore_exceptions == (ValueError, RuntimeError)


def test_ignore_exceptions_accepts_fqn_string() -> None:
    """`ignore_exceptions="builtins.ValueError"` resolves via `ImportString`."""
    cfg = ConsecutiveCountConfig(ignore_exceptions="builtins.ValueError")  # ty: ignore[invalid-argument-type]
    assert cfg.ignore_exceptions == (ValueError,)


def test_factory_accepts_fqn_string_for_ignore_exceptions() -> None:
    """`.consecutive_count(ignore_exceptions="...")` works end-to-end."""
    cb = CircuitBreaker.consecutive_count(
        "payments", ignore_exceptions="builtins.ValueError"
    )
    assert cb.config.ignore_exceptions == (ValueError,)


def test_factory_accepts_mixed_class_and_fqn() -> None:
    """A tuple mixing class refs and FQN strings resolves consistently."""
    cb = CircuitBreaker.consecutive_count(
        "payments",
        ignore_exceptions=(ValueError, "builtins.RuntimeError"),
    )
    assert cb.config.ignore_exceptions == (ValueError, RuntimeError)


def test_ignore_exceptions_accepts_tuple_of_fqn_strings() -> None:
    """A tuple of FQN strings resolves to a tuple of classes."""
    cfg = ConsecutiveCountConfig(
        ignore_exceptions=("builtins.ValueError", "builtins.RuntimeError"),  # ty: ignore[invalid-argument-type]
    )
    assert cfg.ignore_exceptions == (ValueError, RuntimeError)


def test_invalid_threshold_raises() -> None:
    """Non-positive threshold values raise `ValidationError`."""
    with pytest.raises(ValidationError):
        CircuitBreaker.consecutive_count("payments", error_threshold=0)
