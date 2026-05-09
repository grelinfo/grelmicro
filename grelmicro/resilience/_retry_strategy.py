"""Retry strategy factory."""

from typing import assert_never

from grelmicro.resilience._protocol import RetryStrategy
from grelmicro.resilience.backoffs import (
    ConstantBackoffConfig,
    ExponentialBackoffConfig,
    FibonacciBackoffConfig,
    LinearBackoffConfig,
    RandomBackoffConfig,
    RetryBackoffConfig,
)
from grelmicro.resilience.backoffs.constant import _ConstantStrategy
from grelmicro.resilience.backoffs.exponential import _ExponentialStrategy
from grelmicro.resilience.backoffs.fibonacci import _FibonacciStrategy
from grelmicro.resilience.backoffs.linear import _LinearStrategy
from grelmicro.resilience.backoffs.random import _RandomStrategy


def build_retry_strategy(config: RetryBackoffConfig) -> RetryStrategy:
    """Build a fresh strategy bound to ``config``.

    Called once per retry loop. Strategies are stateful (for
    decorrelated jitter and Fibonacci) so each loop gets its own.
    """
    match config:
        case ExponentialBackoffConfig():
            return _ExponentialStrategy(config)
        case ConstantBackoffConfig():
            return _ConstantStrategy(config)
        case LinearBackoffConfig():
            return _LinearStrategy(config)
        case FibonacciBackoffConfig():
            return _FibonacciStrategy(config)
        case RandomBackoffConfig():
            return _RandomStrategy(config)
        case _ as unknown:  # pragma: no cover
            assert_never(unknown)
