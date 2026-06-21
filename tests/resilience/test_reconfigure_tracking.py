"""Reconfigure-tracking tests for the resilience policies.

A policy built from per-field kwargs (or env) registers for external
reconfiguration. A policy built from a pre-built config stays static. The
broader suite exercises `reconfigure()` itself but not this registration, so a
flip of the `config is None` guard in the constructors goes unnoticed.
"""

from grelmicro._config import reconfigurable_instances
from grelmicro.resilience import (
    Bulkhead,
    BulkheadConfig,
    ConstantBackoff,
    Fallback,
    FallbackConfig,
    Match,
    Retry,
    RetryConfig,
    Timeout,
    TimeoutConfig,
)


def test_timeout_kwargs_built_is_tracked() -> None:
    """A kwargs-built `Timeout` registers for external reconfigure."""
    policy = Timeout("track-timeout", seconds=1.0)
    assert policy._env_prefix is not None
    assert policy in reconfigurable_instances()


def test_timeout_prebuilt_config_is_static() -> None:
    """A `Timeout` built from a pre-built config stays static."""
    policy = Timeout.from_config("static-timeout", TimeoutConfig(seconds=1.0))
    assert policy._env_prefix is None
    assert policy not in reconfigurable_instances()


def test_bulkhead_kwargs_built_is_tracked() -> None:
    """A kwargs-built `Bulkhead` registers for external reconfigure."""
    policy = Bulkhead("track-bulkhead", max_concurrent=2)
    assert policy._env_prefix is not None
    assert policy in reconfigurable_instances()


def test_bulkhead_prebuilt_config_is_static() -> None:
    """A `Bulkhead` built from a pre-built config stays static."""
    policy = Bulkhead.from_config(
        "static-bulkhead", BulkheadConfig(max_concurrent=2)
    )
    assert policy._env_prefix is None
    assert policy not in reconfigurable_instances()


def test_fallback_kwargs_built_is_tracked() -> None:
    """A kwargs-built `Fallback` registers for external reconfigure."""
    policy = Fallback("track-fallback", when=ValueError, default=None)
    assert policy._env_prefix is not None
    assert policy in reconfigurable_instances()


def test_fallback_prebuilt_config_is_static() -> None:
    """A `Fallback` built from a pre-built config stays static."""
    policy = Fallback.from_config(
        "static-fallback",
        FallbackConfig(when=Match.exception(ValueError), default=None),
    )
    assert policy._env_prefix is None
    assert policy not in reconfigurable_instances()


def test_retry_kwargs_built_is_tracked() -> None:
    """A kwargs-built `Retry` registers for external reconfigure."""
    policy = Retry(
        "track-retry", ConstantBackoff(delay=1.0), when=ValueError
    )
    assert policy._env_prefix is not None
    assert policy in reconfigurable_instances()


def test_retry_prebuilt_config_is_static() -> None:
    """A `Retry` built from a pre-built config stays static."""
    policy = Retry.from_config("static-retry", RetryConfig(when=(ValueError,)))
    assert policy._env_prefix is None
    assert policy not in reconfigurable_instances()
