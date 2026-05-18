"""Token Bucket algorithm configuration."""

from typing import Annotated, Literal

from pydantic import PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro.resilience.ratelimiter._base import _BaseRateLimiterConfig


class TokenBucketConfig(_BaseRateLimiterConfig, frozen=True, extra="forbid"):
    """Classic token bucket rate-limiting algorithm.

    The bucket starts full and refills continuously at
    `refill_rate` tokens per second, capped at `capacity`. Each
    request consumes tokens. If the bucket has enough, the request
    is allowed, otherwise it is rejected with a `retry_after` hint.

    Use this when you want the pattern "allow a burst of N
    requests, then a steady rate of 1 request per second". The
    token bucket is a common choice for bursty rate limiting.

    Example:
    ```python
    from grelmicro.resilience import RateLimiter, TokenBucketConfig

    # Allow 10 in a burst, then 1/sec sustained.
    rl = RateLimiter("api", TokenBucketConfig(capacity=10, refill_rate=1))
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    kind: Annotated[
        Literal["token_bucket"],
        Doc("Discriminator for the algorithm Pydantic union."),
    ] = "token_bucket"

    capacity: Annotated[
        PositiveInt,
        Doc(
            "Maximum burst size. The bucket never holds more than "
            "`capacity` tokens."
        ),
    ]

    refill_rate: Annotated[
        PositiveFloat,
        Doc("Tokens replenished per second, up to `capacity`."),
    ]
