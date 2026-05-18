"""Sliding-window rate-limiter configuration."""

from typing import Annotated, Literal

from pydantic import PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro.resilience.ratelimiter._base import _BaseRateLimiterConfig


class SlidingWindowConfig(_BaseRateLimiterConfig, frozen=True, extra="forbid"):
    """Precise sliding-window rate limiting.

    Stores a single timestamp per key (about 72 bytes).

    Use this when you need a precise sliding window, such as for
    HTTP API throttling with RFC 9211 `RateLimiters-*` headers or
    legacy `X-RateLimiters-*` headers. For the pattern "allow a burst
    of N, then 1 per second", use
    [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
    instead.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, SlidingWindowConfig

    # 5 requests per 60-second sliding window.
    rl = RateLimiter("auth", SlidingWindowConfig(limit=5, window=60))
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    kind: Annotated[
        Literal["sliding_window"],
        Doc("Discriminator for the algorithm Pydantic union."),
    ] = "sliding_window"

    limit: Annotated[
        PositiveInt,
        Doc("Maximum number of requests allowed per window."),
    ]

    window: Annotated[
        PositiveFloat,
        Doc("Window duration in seconds."),
    ]
