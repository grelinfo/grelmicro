"""Exponential backoff strategy tests."""

import random

import pytest
from pydantic import ValidationError

from grelmicro.resilience.backoffs.exponential import (
    ExponentialBackoffConfig,
    _ExponentialStrategy,
)

_DEFAULT_BASE = 0.1
_DEFAULT_MAX = 30.0
_NO_JITTER_DELAYS = [0.1, 0.2, 0.4, 0.8, 1.6]
_CAPPED_BASE = 1.0
_CAPPED_MAX = 4.0


def test_default_config() -> None:
    """Default config: type=exponential, base=0.1, max=30, jitter=full."""
    config = ExponentialBackoffConfig()
    assert config.type == "exponential"
    assert config.base_delay == _DEFAULT_BASE
    assert config.max_delay == _DEFAULT_MAX
    assert config.jitter == "full"


def test_no_jitter_doubles_per_attempt() -> None:
    """Without jitter, delay doubles each attempt."""
    config = ExponentialBackoffConfig(
        base_delay=_DEFAULT_BASE, max_delay=10.0, jitter="none"
    )
    strategy = _ExponentialStrategy(config)
    delays = [strategy.delay(n) for n in range(1, 6)]
    assert delays == _NO_JITTER_DELAYS


def test_no_jitter_capped_at_max_delay() -> None:
    """Delay caps at `max_delay`."""
    config = ExponentialBackoffConfig(
        base_delay=_CAPPED_BASE, max_delay=_CAPPED_MAX, jitter="none"
    )
    strategy = _ExponentialStrategy(config)
    assert strategy.delay(1) == _CAPPED_BASE
    assert strategy.delay(2) == _CAPPED_BASE * 2
    assert strategy.delay(3) == _CAPPED_MAX
    assert strategy.delay(10) == _CAPPED_MAX


def test_full_jitter_returns_value_within_bounds() -> None:
    """Full jitter samples from `[0, raw_delay]`."""
    config = ExponentialBackoffConfig(
        base_delay=_DEFAULT_BASE, max_delay=10.0, jitter="full"
    )
    strategy = _ExponentialStrategy(config)
    random.seed(42)
    for n in range(1, 6):
        d = strategy.delay(n)
        upper = min(_DEFAULT_BASE * (2 ** (n - 1)), 10.0)
        assert 0.0 <= d <= upper


def test_equal_jitter_within_bounds() -> None:
    """Equal jitter samples in `[raw/2, raw]`."""
    config = ExponentialBackoffConfig(
        base_delay=_DEFAULT_BASE, max_delay=10.0, jitter="equal"
    )
    strategy = _ExponentialStrategy(config)
    for n in range(1, 6):
        raw = min(_DEFAULT_BASE * (2 ** (n - 1)), 10.0)
        d = strategy.delay(n)
        assert raw / 2 <= d <= raw


_DECORR_MAX = 5.0


def test_decorrelated_jitter_within_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decorrelated jitter stays within `[base_delay, max_delay]`."""
    config = ExponentialBackoffConfig(
        base_delay=_DEFAULT_BASE, max_delay=_DECORR_MAX, jitter="decorrelated"
    )
    strategy = _ExponentialStrategy(config)
    monkeypatch.setattr("random.uniform", lambda lo, hi: (lo + hi) / 2)
    delays = [strategy.delay(n) for n in range(1, 6)]
    for d in delays:
        assert _DEFAULT_BASE <= d <= _DECORR_MAX


def test_frozen_config() -> None:
    """`ExponentialBackoffConfig` is frozen."""
    config = ExponentialBackoffConfig()
    with pytest.raises(ValidationError):
        config.base_delay = 0.5  # type: ignore[misc]  # ty: ignore[invalid-assignment]
