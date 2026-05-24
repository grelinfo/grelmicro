"""Exponential backoff configuration."""

import random
from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class ExponentialBackoff(BaseModel, frozen=True, extra="forbid"):
    """Exponential backoff with optional jitter.

    The raw delay before retry ``N`` is
    ``min(base_delay * 2 ** (N - 1), max_delay)``: it doubles each
    attempt until it reaches the cap. ``jitter`` then transforms
    that raw delay so concurrent callers do not retry in lockstep
    (the actual sleep may be smaller than the raw value).

    Example:
    ```python
    from grelmicro.resilience import ExponentialBackoff, Retry

    policy = Retry(
        "payments",
        ExponentialBackoff(base_delay=0.2, max_delay=10.0),
        when=httpx.HTTPError,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    kind: Annotated[
        Literal["exponential"],
        Doc("Discriminator for the backoff Pydantic union."),
    ] = "exponential"

    base_delay: Annotated[
        PositiveFloat,
        Doc("Initial delay in seconds before the first retry."),
    ] = 0.1

    max_delay: Annotated[
        PositiveFloat,
        Doc("Maximum delay in seconds. Caps the exponential growth."),
    ] = 30.0

    jitter: Annotated[
        Literal["none", "full", "equal", "decorrelated"],
        Doc(
            "Jitter mode. ``full`` (default) samples from "
            "``[0, raw]`` and is the safest choice against retry "
            "storms. ``equal`` samples from ``[raw/2, raw]`` and "
            "keeps timing more predictable. ``decorrelated`` "
            "chains samples across attempts and is best for many "
            "clients hitting the same recovering dependency. "
            "``none`` disables jitter."
        ),
    ] = "full"


class _ExponentialStrategy:
    """Stateful exponential backoff strategy.

    Holds the previous delay so ``decorrelated`` jitter can chain
    samples. One strategy is built per retry loop.
    """

    __slots__ = ("_config", "_previous")

    def __init__(self, config: ExponentialBackoff) -> None:
        self._config = config
        self._previous: float = config.base_delay

    def delay(self, attempt: int) -> float:
        config = self._config
        raw = min(config.base_delay * (2 ** (attempt - 1)), config.max_delay)
        match config.jitter:
            case "none":
                jittered = raw
            case "full":
                jittered = random.uniform(0.0, raw)  # noqa: S311
            case "equal":
                half = raw / 2
                jittered = half + random.uniform(0.0, half)  # noqa: S311
            case "decorrelated":  # pragma: no branch
                jittered = min(
                    config.max_delay,
                    random.uniform(  # noqa: S311
                        config.base_delay, self._previous * 3
                    ),
                )
        self._previous = max(jittered, config.base_delay)
        return jittered
