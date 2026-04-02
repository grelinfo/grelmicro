"""Test Resilience Errors."""

import subprocess
import sys
import warnings
from datetime import UTC, datetime

import pytest

import grelmicro.resilience as resilience_mod
import grelmicro.resilience.errors as errors_mod
from grelmicro.resilience.errors import CircuitBreakerError


def test_circuit_breaker_error() -> None:
    """Test CircuitBreakerError."""
    # Arrange
    time = datetime.now(tz=UTC)
    exc = Exception("This is a test error")

    # Act
    error = CircuitBreakerError(
        name="test",
        last_error_time=time,
        last_error=exc,
    )

    # Assert
    assert str(error) == "Circuit breaker 'test' call not permitted"
    assert error.last_error == exc
    assert error.last_error_time == time


def test_resilience_module_exports() -> None:
    """Test resilience module __all__ contains expected symbols."""
    expected = {
        "CircuitBreaker",
        "CircuitBreakerError",
        "CircuitBreakerMetrics",
        "CircuitBreakerState",
        "ErrorDetails",
        "RateLimitExceededError",
        "RateLimitResult",
        "RateLimiter",
        "RateLimiterBackend",
        "RateLimiterConfig",
        "ResilienceError",
        "ResilienceSettingsValidationError",
    }
    assert set(resilience_mod.__all__) == expected


def test_resilience_exception_deprecated_alias_from_module() -> None:
    """Test ResilienceException alias emits DeprecationWarning from module."""
    resilience_mod.__dict__.pop("ResilienceException", None)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cls = resilience_mod.ResilienceException
        assert cls is resilience_mod.ResilienceError
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "ResilienceException" in str(w[0].message)


def test_resilience_exception_deprecated_alias_from_errors() -> None:
    """Test ResilienceException alias emits DeprecationWarning from errors module."""
    errors_mod.__dict__.pop("ResilienceException", None)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cls = errors_mod.ResilienceException
        assert cls is errors_mod.ResilienceError
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)


def test_resilience_module_getattr_unknown() -> None:
    """Test __getattr__ raises AttributeError for unknown names."""
    with pytest.raises(AttributeError, match="NoSuchThing"):
        resilience_mod.NoSuchThing  # noqa: B018


def test_resilience_errors_getattr_unknown() -> None:
    """Test errors __getattr__ raises AttributeError for unknown names."""
    with pytest.raises(AttributeError, match="NoSuchThing"):
        errors_mod.NoSuchThing  # noqa: B018


def test_resilience_exception_from_import_single_warning() -> None:
    """Test 'from grelmicro.resilience import ResilienceException' emits exactly one warning.

    Regression test: CPython's importlib._handle_fromlist calls __getattr__
    twice internally. The globals() caching prevents duplicate warnings.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "always",
            "-c",
            "from grelmicro.resilience import ResilienceException",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    warning_lines = [
        line
        for line in result.stderr.splitlines()
        if "DeprecationWarning" in line
    ]
    assert len(warning_lines) == 1, (
        f"Expected 1 warning, got {len(warning_lines)}: {result.stderr}"
    )
