"""Constant backoff strategy tests."""

import pytest
from pydantic import ValidationError

from grelmicro.resilience.backoffs.constant import (
    ConstantBackoffConfig,
    _ConstantStrategy,
)

_DEFAULT_DELAY = 1.0
_CUSTOM_DELAY = 0.5
_NEW_DELAY = 2.0


def test_default_config() -> None:
    """Default config: type=constant, delay=1.0."""
    config = ConstantBackoffConfig()
    assert config.type == "constant"
    assert config.delay == _DEFAULT_DELAY


def test_strategy_returns_constant_value() -> None:
    """The strategy returns the configured delay regardless of attempt."""
    strategy = _ConstantStrategy(ConstantBackoffConfig(delay=_CUSTOM_DELAY))
    assert strategy.delay(1) == _CUSTOM_DELAY
    assert strategy.delay(2) == _CUSTOM_DELAY


def test_frozen_config() -> None:
    """`ConstantBackoffConfig` is frozen."""
    config = ConstantBackoffConfig()
    with pytest.raises(ValidationError):
        config.delay = _NEW_DELAY  # type: ignore[misc]  # ty: ignore[invalid-assignment]
