"""Tests for the three-paths CircuitBreaker construction."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
)

ERROR_KWARG = 7
ERROR_ENV = 12
RESET_ENV = 99.0
DEFAULT_ERROR = 5
DEFAULT_SUCCESS = 2
DEFAULT_RESET = 30.0
DEFAULT_HALF_OPEN_CAPACITY = 1
DEFAULT_LOG_LEVEL = "WARNING"


def test_programmatic_path_uses_kwargs() -> None:
    """Plain kwargs build a config, falling back to CircuitBreakerConfig defaults."""
    cb = CircuitBreaker("payments", error_threshold=ERROR_KWARG)
    assert cb.name == "payments"
    assert cb.config.error_threshold == ERROR_KWARG
    assert cb.config.success_threshold == DEFAULT_SUCCESS


def test_declarative_path_uses_from_config() -> None:
    """`CircuitBreaker.from_config()` constructs from a name and a `CircuitBreakerConfig`."""
    cfg = CircuitBreakerConfig(error_threshold=ERROR_KWARG, reset_timeout=10.0)
    cb = CircuitBreaker.from_config("payments", cfg)
    assert cb.name == "payments"
    assert cb.config is cfg


def test_from_config_bypasses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CircuitBreaker.from_config()` ignores env even when set."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD", str(ERROR_ENV)
    )
    cfg = CircuitBreakerConfig(error_threshold=ERROR_KWARG)
    cb = CircuitBreaker.from_config("payments", cfg)
    assert cb.config.error_threshold == ERROR_KWARG


def test_environmental_path_reads_grel_prefixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars under ``GREL_CIRCUIT_BREAKER_{NAME}_*`` populate unset fields."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD", str(ERROR_ENV)
    )
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_RESET_TIMEOUT", str(RESET_ENV)
    )
    cb = CircuitBreaker("payments")
    assert cb.config.error_threshold == ERROR_ENV
    assert cb.config.reset_timeout == RESET_ENV


def test_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller kwargs win over env vars."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD", str(ERROR_ENV)
    )
    cb = CircuitBreaker("payments", error_threshold=ERROR_KWARG)
    assert cb.config.error_threshold == ERROR_KWARG


def test_env_prefix_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env_prefix=`` replaces the auto-derived ``GREL_CIRCUIT_BREAKER_{NAME}_``."""
    monkeypatch.setenv(
        "APP_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD", str(ERROR_ENV)
    )
    cb = CircuitBreaker("payments", env_prefix="APP_CIRCUIT_BREAKER_PAYMENTS_")
    assert cb.config.error_threshold == ERROR_ENV


def test_env_load_false_ignores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env_load=False`` skips env reads entirely."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD", str(ERROR_ENV)
    )
    cb = CircuitBreaker("payments", env_load=False)
    assert cb.config.error_threshold == DEFAULT_ERROR


def test_zero_config_uses_circuitbreakerconfig_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without env or kwargs, CircuitBreakerConfig defaults take over."""
    monkeypatch.delenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_ERROR_THRESHOLD", raising=False
    )
    cb = CircuitBreaker("payments")
    assert cb.config.error_threshold == DEFAULT_ERROR
    assert cb.config.success_threshold == DEFAULT_SUCCESS
    assert cb.config.reset_timeout == DEFAULT_RESET
    assert cb.config.half_open_capacity == DEFAULT_HALF_OPEN_CAPACITY
    assert cb.config.log_level == DEFAULT_LOG_LEVEL


def test_ignore_exceptions_accepts_single_class() -> None:
    """`ignore_exceptions=SomeError` is accepted as shorthand for `(SomeError,)`."""
    cb = CircuitBreaker("payments", ignore_exceptions=ValueError)
    assert cb.config.ignore_exceptions == (ValueError,)


def test_ignore_exceptions_accepts_tuple() -> None:
    """`ignore_exceptions=(A, B)` is preserved."""
    cb = CircuitBreaker(
        "payments", ignore_exceptions=(ValueError, RuntimeError)
    )
    assert cb.config.ignore_exceptions == (ValueError, RuntimeError)


def test_ignore_exceptions_accepts_fqn_string() -> None:
    """`ignore_exceptions="builtins.ValueError"` resolves via `ImportString`."""
    cfg = CircuitBreakerConfig(ignore_exceptions="builtins.ValueError")  # ty: ignore[invalid-argument-type]
    assert cfg.ignore_exceptions == (ValueError,)


def test_constructor_accepts_fqn_string_for_ignore_exceptions() -> None:
    """`CircuitBreaker("...", ignore_exceptions="...")` works end-to-end."""
    cb = CircuitBreaker("payments", ignore_exceptions="builtins.ValueError")
    assert cb.config.ignore_exceptions == (ValueError,)


def test_constructor_accepts_mixed_class_and_fqn() -> None:
    """A tuple mixing class refs and FQN strings resolves consistently."""
    cb = CircuitBreaker(
        "payments",
        ignore_exceptions=(ValueError, "builtins.RuntimeError"),
    )
    assert cb.config.ignore_exceptions == (ValueError, RuntimeError)


def test_ignore_exceptions_accepts_tuple_of_fqn_strings() -> None:
    """A tuple of FQN strings resolves to a tuple of classes."""
    cfg = CircuitBreakerConfig(
        ignore_exceptions=("builtins.ValueError", "builtins.RuntimeError"),  # ty: ignore[invalid-argument-type]
    )
    assert cfg.ignore_exceptions == (ValueError, RuntimeError)


def test_ignore_exceptions_from_env_accepts_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var supports CSV format for operator-friendly shell input."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_IGNORE_EXCEPTIONS",
        "builtins.ValueError,builtins.RuntimeError",
    )
    cb = CircuitBreaker("payments")
    assert cb.config.ignore_exceptions == (ValueError, RuntimeError)


def test_ignore_exceptions_from_env_accepts_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var also accepts a JSON array for back-compat."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_IGNORE_EXCEPTIONS",
        '["builtins.ValueError","builtins.RuntimeError"]',
    )
    cb = CircuitBreaker("payments")
    assert cb.config.ignore_exceptions == (ValueError, RuntimeError)


def test_invalid_threshold_raises() -> None:
    """Non-positive threshold values raise `ValidationError`."""
    with pytest.raises(ValidationError):
        CircuitBreaker("payments", error_threshold=0)


def test_name_with_punctuation_normalises_env_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name with punctuation normalises into a valid env prefix."""
    monkeypatch.setenv(
        "GREL_CIRCUIT_BREAKER_PAYMENTS_EU_ERROR_THRESHOLD", str(ERROR_ENV)
    )
    cb = CircuitBreaker("payments-eu")
    assert cb.config.error_threshold == ERROR_ENV
    assert cb.name == "payments-eu"
