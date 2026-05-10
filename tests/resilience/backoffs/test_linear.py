"""Linear backoff strategy tests."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.backoffs.linear import (
    LinearBackoff,
    _LinearStrategy,
)

_DEFAULT_BASE = 1.0
_DEFAULT_MAX = 30.0


def test_default_config() -> None:
    """Default config: type=linear, base=1.0, max=30.0."""
    config = LinearBackoff()
    assert config.type == "linear"
    assert config.base_delay == _DEFAULT_BASE
    assert config.max_delay == _DEFAULT_MAX


def test_strategy_grows_linearly() -> None:
    """Delays follow ``base * N``."""
    strategy = _LinearStrategy(LinearBackoff(base_delay=2.0, max_delay=100.0))
    delays = [strategy.delay(n) for n in range(1, 6)]
    assert delays == [2.0, 4.0, 6.0, 8.0, 10.0]


_CAP_BASE = 1.0
_CAP_MAX = 3.0


def test_strategy_caps_at_max_delay() -> None:
    """Delay caps at ``max_delay``."""
    strategy = _LinearStrategy(
        LinearBackoff(base_delay=_CAP_BASE, max_delay=_CAP_MAX)
    )
    assert strategy.delay(1) == _CAP_BASE
    assert strategy.delay(3) == _CAP_MAX
    assert strategy.delay(10) == _CAP_MAX


def test_frozen_config() -> None:
    """`LinearBackoff` is frozen."""
    config = LinearBackoff()
    with pytest.raises(ValidationError):
        config.base_delay = 5.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]
