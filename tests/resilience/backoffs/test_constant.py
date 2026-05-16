"""Constant backoff strategy tests."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.backoffs.constant import (
    ConstantBackoff,
    _ConstantStrategy,
)

_DEFAULT_DELAY = 1.0
_CUSTOM_DELAY = 0.5
_NEW_DELAY = 2.0


def test_default_config() -> None:
    """Default config: kind=constant, delay=1.0."""
    config = ConstantBackoff()
    assert config.kind == "constant"
    assert config.delay == _DEFAULT_DELAY


def test_strategy_returns_constant_value() -> None:
    """The strategy returns the configured delay regardless of attempt."""
    strategy = _ConstantStrategy(ConstantBackoff(delay=_CUSTOM_DELAY))
    assert strategy.delay(1) == _CUSTOM_DELAY
    assert strategy.delay(2) == _CUSTOM_DELAY


def test_frozen_config() -> None:
    """`ConstantBackoff` is frozen."""
    config = ConstantBackoff()
    with pytest.raises(ValidationError):
        config.delay = _NEW_DELAY  # type: ignore[misc]  # ty: ignore[invalid-assignment]
