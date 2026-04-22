"""GCRA (Generic Cell Rate Algorithm) configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc


class GCRA(BaseModel, frozen=True, extra="forbid"):
    """Generic Cell Rate Algorithm: sliding-window rate limiting.

    Stores a single timestamp per key (about 72 bytes). It is
    mathematically equivalent to the "leaky bucket" algorithm.
    If you are looking for a "leaky bucket" rate limiter, use
    `GCRA`.

    Use this when you need a precise sliding window, such as
    for HTTP API throttling with `X-RateLimit-*` headers. For
    the pattern "allow a burst of N, then 1 per second", use
    [`TokenBucket`][grelmicro.resilience.algorithms.TokenBucket]
    instead.

    Example:
    ```python
    from grelmicro.resilience import GCRA, RateLimiter

    # 5 requests per 60-second sliding window.
    rl = RateLimiter("auth", algorithm=GCRA(limit=5, window=60))
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
