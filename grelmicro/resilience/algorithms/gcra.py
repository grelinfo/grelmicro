"""GCRA (Generic Cell Rate Algorithm) configuration."""

from typing import Annotated, Literal

from pydantic import PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro.resilience.algorithms._base import _BaseRateLimiterConfig


class GCRAConfig(_BaseRateLimiterConfig, frozen=True, extra="forbid"):
    """Generic Cell Rate Algorithm: sliding-window rate limiting.

    Stores a single timestamp per key (about 72 bytes). It is
    mathematically equivalent to the "leaky bucket" algorithm.
    If you are looking for a "leaky bucket" rate limiter, use
    `GCRAConfig`.

    Use this when you need a precise sliding window, such as
    for HTTP API throttling with RFC 9211 `RateLimit-*` headers
    or legacy `X-RateLimit-*` headers. For the pattern "allow a
    burst of N, then 1 per second", use
    [`TokenBucketConfig`][grelmicro.resilience.algorithms.TokenBucketConfig]
    instead.

    Example:
    ```python
    from grelmicro.resilience import GCRAConfig, RateLimiter

    # 5 requests per 60-second sliding window.
    rl = RateLimiter("auth", GCRAConfig(limit=5, window=60))
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    type: Annotated[
        Literal["gcra"],
        Doc("Discriminator for the algorithm Pydantic union."),
    ] = "gcra"

    limit: Annotated[
        PositiveInt,
        Doc("Maximum number of requests allowed per window."),
    ]

    window: Annotated[
        PositiveFloat,
        Doc("Window duration in seconds."),
    ]
