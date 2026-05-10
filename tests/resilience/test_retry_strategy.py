"""Tests for the backoff strategy factory."""

from grelmicro.resilience._retry_strategy import build_retry_strategy
from grelmicro.resilience.backoffs.constant import (
    ConstantBackoff,
    _ConstantStrategy,
)
from grelmicro.resilience.backoffs.exponential import (
    ExponentialBackoff,
    _ExponentialStrategy,
)
from grelmicro.resilience.backoffs.fibonacci import (
    FibonacciBackoff,
    _FibonacciStrategy,
)
from grelmicro.resilience.backoffs.linear import (
    LinearBackoff,
    _LinearStrategy,
)
from grelmicro.resilience.backoffs.random import (
    RandomBackoff,
    _RandomStrategy,
)


def test_build_exponential() -> None:
    """`build_retry_strategy` dispatches to the exponential strategy."""
    strategy = build_retry_strategy(ExponentialBackoff())
    assert isinstance(strategy, _ExponentialStrategy)


def test_build_constant() -> None:
    """`build_retry_strategy` dispatches to the constant strategy."""
    strategy = build_retry_strategy(ConstantBackoff())
    assert isinstance(strategy, _ConstantStrategy)


def test_build_linear() -> None:
    """`build_retry_strategy` dispatches to the linear strategy."""
    strategy = build_retry_strategy(LinearBackoff())
    assert isinstance(strategy, _LinearStrategy)


def test_build_fibonacci() -> None:
    """`build_retry_strategy` dispatches to the Fibonacci strategy."""
    strategy = build_retry_strategy(FibonacciBackoff())
    assert isinstance(strategy, _FibonacciStrategy)


def test_build_random() -> None:
    """`build_retry_strategy` dispatches to the random strategy."""
    strategy = build_retry_strategy(RandomBackoff())
    assert isinstance(strategy, _RandomStrategy)
