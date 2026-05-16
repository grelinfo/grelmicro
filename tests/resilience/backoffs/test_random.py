"""Random backoff strategy tests."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.backoffs.random import (
    RandomBackoff,
    _RandomStrategy,
)

_DEFAULT_MIN = 0.5
_DEFAULT_MAX = 2.0


def test_default_config() -> None:
    """Default config: kind=random, min=0.5, max=2.0."""
    config = RandomBackoff()
    assert config.kind == "random"
    assert config.min_delay == _DEFAULT_MIN
    assert config.max_delay == _DEFAULT_MAX


_SAMPLES = 1000
_TEST_MIN = 1.0
_TEST_MAX = 3.0


def test_strategy_returns_value_in_range() -> None:
    """Each delay is uniform random in `[min, max]` (sample-distribution test)."""
    strategy = _RandomStrategy(
        RandomBackoff(min_delay=_TEST_MIN, max_delay=_TEST_MAX)
    )
    samples = {strategy.delay(1) for _ in range(_SAMPLES)}
    assert len(samples) > 1
    for d in samples:
        assert _TEST_MIN <= d <= _TEST_MAX


def test_min_must_not_exceed_max() -> None:
    """`min_delay` must be `<= max_delay`."""
    with pytest.raises(ValidationError, match="min_delay"):
        RandomBackoff(min_delay=5.0, max_delay=1.0)


def test_frozen_config() -> None:
    """`RandomBackoff` is frozen."""
    config = RandomBackoff()
    with pytest.raises(ValidationError):
        config.min_delay = 1.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]
