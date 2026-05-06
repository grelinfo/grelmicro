"""Tests for the backoff strategy factory."""

from grelmicro.resilience._retry_strategy import build_retry_strategy
from grelmicro.resilience.backoffs.constant import (
    ConstantBackoffConfig,
    _ConstantStrategy,
)
from grelmicro.resilience.backoffs.exponential import (
    ExponentialBackoffConfig,
    _ExponentialStrategy,
)
from grelmicro.resilience.backoffs.fibonacci import (
    FibonacciBackoffConfig,
    _FibonacciStrategy,
)
from grelmicro.resilience.backoffs.linear import (
    LinearBackoffConfig,
    _LinearStrategy,
)
from grelmicro.resilience.backoffs.random import (
    RandomBackoffConfig,
    _RandomStrategy,
)


def test_build_exponential() -> None:
    """`build_retry_strategy` dispatches to the exponential strategy."""
    strategy = build_retry_strategy(ExponentialBackoffConfig())
    assert isinstance(strategy, _ExponentialStrategy)


def test_build_constant() -> None:
    """`build_retry_strategy` dispatches to the constant strategy."""
    strategy = build_retry_strategy(ConstantBackoffConfig())
    assert isinstance(strategy, _ConstantStrategy)


def test_build_linear() -> None:
    """`build_retry_strategy` dispatches to the linear strategy."""
    strategy = build_retry_strategy(LinearBackoffConfig())
    assert isinstance(strategy, _LinearStrategy)


def test_build_fibonacci() -> None:
    """`build_retry_strategy` dispatches to the Fibonacci strategy."""
    strategy = build_retry_strategy(FibonacciBackoffConfig())
    assert isinstance(strategy, _FibonacciStrategy)


def test_build_random() -> None:
    """`build_retry_strategy` dispatches to the random strategy."""
    strategy = build_retry_strategy(RandomBackoffConfig())
    assert isinstance(strategy, _RandomStrategy)
