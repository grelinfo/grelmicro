"""GCRA (Generic Cell Rate Algorithm) configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc


class GCRA(BaseModel, frozen=True, extra="forbid"):
    """Generic Cell Rate Algorithm: sliding-window rate limiting.

    Tracks a single theoretical arrival time (TAT) per key
    (~72 bytes). Mathematically equivalent to the "leaky bucket as
    meter" formulation used by Stripe; operators searching for a
    "leaky bucket" rate limiter should use ``GCRA``.

    Use when you need precise sliding-window semantics (e.g. HTTP
    API throttling with ``X-RateLimit-*`` headers). For
    burst-friendly "allow N, then 1/sec" semantics use
    :class:`~grelmicro.resilience.algorithms.tokenbucket.TokenBucket`
    instead.
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
