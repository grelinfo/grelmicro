"""Exponential backoff configuration."""

import random
from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class ExponentialBackoffConfig(BaseModel, frozen=True, extra="forbid"):
    """Exponential backoff with optional jitter.

    Each retry waits twice as long as the previous one, capped at
    ``max_delay``. The raw delay is then transformed by ``jitter``
    so concurrent callers do not retry in lockstep.

    Example:
    ```python
    from grelmicro.resilience import ExponentialBackoffConfig, Retry

    policy = Retry(
        "payments",
        ExponentialBackoffConfig(base_delay=0.2, max_delay=10.0),
        on=httpx.HTTPError,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    type: Annotated[
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

    def __init__(self, config: ExponentialBackoffConfig) -> None:
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
            case "decorrelated":
                jittered = min(
                    config.max_delay,
                    random.uniform(  # noqa: S311
                        config.base_delay, self._previous * 3
                    ),
                )
        self._previous = max(jittered, config.base_delay)
        return jittered
