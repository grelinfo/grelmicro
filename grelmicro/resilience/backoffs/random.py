"""Random backoff configuration."""

import random as _random
from typing import Annotated, Literal, Self

from pydantic import BaseModel, PositiveFloat, model_validator
from typing_extensions import Doc


class RandomBackoff(BaseModel, frozen=True, extra="forbid"):
    """Random backoff: each delay is uniform random in ``[min_delay, max_delay]``.

    Use this when you want bounded random spread without progressive
    growth. Common for cache-stampede protection (many clients miss
    the cache simultaneously: each waits a random short interval to
    avoid hammering the origin together).

    For HTTP retries, prefer
    [`ExponentialBackoff`][grelmicro.resilience.ExponentialBackoff]
    with jitter. Random alone does not back off, so a persistent
    failure retries at the same average rate forever.

    Example:
    ```python
    from grelmicro.resilience import RandomBackoff, Retry

    # Each retry waits a random 0.5-2.0 seconds
    policy = Retry(
        "stampede",
        RandomBackoff(min_delay=0.5, max_delay=2.0),
        on=CacheMissError,
        attempts=3,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    kind: Annotated[
        Literal["random"],
        Doc("Discriminator for the backoff Pydantic union."),
    ] = "random"

    min_delay: Annotated[
        PositiveFloat,
        Doc("Minimum delay in seconds (inclusive)."),
    ] = 0.5

    max_delay: Annotated[
        PositiveFloat,
        Doc("Maximum delay in seconds (inclusive)."),
    ] = 2.0

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.min_delay > self.max_delay:
            msg = (
                f"min_delay ({self.min_delay}) must be <= "
                f"max_delay ({self.max_delay})"
            )
            raise ValueError(msg)
        return self


class _RandomStrategy:
    """Stateless random backoff strategy."""

    __slots__ = ("_max", "_min")

    def __init__(self, config: RandomBackoff) -> None:
        self._min = config.min_delay
        self._max = config.max_delay

    def delay(self, attempt: int) -> float:
        del attempt
        return _random.uniform(self._min, self._max)  # noqa: S311
