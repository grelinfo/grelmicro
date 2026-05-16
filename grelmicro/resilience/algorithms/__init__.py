"""Rate-limiter algorithm configurations.

Algorithm configurations are pure [Pydantic](https://docs.pydantic.dev/)
data classes. A [`RateLimiter`][grelmicro.resilience.RateLimiter]
binds an algorithm config to a backend once at construction via
[`RateLimiterBackend.bind`][grelmicro.resilience.RateLimiterBackend.bind].
At runtime the bound strategy is called directly, with no algorithm
dispatch on the hot path.
"""

from typing import Annotated

from pydantic import Discriminator

from grelmicro.resilience.algorithms.sliding_window import SlidingWindowConfig
from grelmicro.resilience.algorithms.tokenbucket import TokenBucketConfig

RateLimiterConfig = Annotated[
    TokenBucketConfig | SlidingWindowConfig,
    Discriminator("type"),
]
"""Discriminated union of supported rate-limiter algorithm configurations."""

__all__ = ["RateLimiterConfig", "SlidingWindowConfig", "TokenBucketConfig"]
