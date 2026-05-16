"""Fibonacci backoff strategy tests."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.backoffs.fibonacci import (
    FibonacciBackoff,
    _FibonacciStrategy,
)

_DEFAULT_BASE = 1.0
_DEFAULT_MAX = 30.0
_FIB_DELAYS = [1.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0]


def test_default_config() -> None:
    """Default config: kind=fibonacci, base=1.0, max=30.0."""
    config = FibonacciBackoff()
    assert config.kind == "fibonacci"
    assert config.base_delay == _DEFAULT_BASE
    assert config.max_delay == _DEFAULT_MAX


def test_strategy_follows_fibonacci_sequence() -> None:
    """Delays follow ``base * fib(N)``: 1, 1, 2, 3, 5, 8, 13."""
    strategy = _FibonacciStrategy(
        FibonacciBackoff(base_delay=1.0, max_delay=100.0)
    )
    delays = [strategy.delay(n) for n in range(1, 8)]
    assert delays == _FIB_DELAYS


_CAP = 4.0


def test_strategy_caps_at_max_delay() -> None:
    """Delay caps at ``max_delay``."""
    strategy = _FibonacciStrategy(
        FibonacciBackoff(base_delay=1.0, max_delay=_CAP)
    )
    # 1, 1, 2, 3, 4 (capped from 5), 4, 4, ...
    delays = [strategy.delay(n) for n in range(1, 8)]
    assert delays[0:4] == [1.0, 1.0, 2.0, 3.0]
    assert all(d == _CAP for d in delays[4:])


def test_frozen_config() -> None:
    """`FibonacciBackoff` is frozen."""
    config = FibonacciBackoff()
    with pytest.raises(ValidationError):
        config.base_delay = 2.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]
