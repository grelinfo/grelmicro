"""Retry strategy factory."""

from typing import assert_never

from grelmicro.resilience._protocol import RetryStrategy
from grelmicro.resilience.backoffs import (
    ConstantBackoffConfig,
    ExponentialBackoffConfig,
    RetryBackoffConfig,
)
from grelmicro.resilience.backoffs.constant import _ConstantStrategy
from grelmicro.resilience.backoffs.exponential import _ExponentialStrategy


def build_retry_strategy(config: RetryBackoffConfig) -> RetryStrategy:
    """Build a fresh strategy bound to ``config``.

    Called once per retry loop. Strategies are stateful (for
    decorrelated jitter) so each loop gets its own.
    """
    match config:
        case ExponentialBackoffConfig():
            return _ExponentialStrategy(config)
        case ConstantBackoffConfig():
            return _ConstantStrategy(config)
        case _ as unknown:  # pragma: no cover
            assert_never(unknown)
