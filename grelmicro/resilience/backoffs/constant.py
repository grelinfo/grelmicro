"""Constant backoff configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class ConstantBackoffConfig(BaseModel, frozen=True, extra="forbid"):
    """Constant delay between retries.

    Use this for polling-style retries where you wait a fixed
    interval. For network and HTTP calls, prefer
    [`ExponentialBackoffConfig`][grelmicro.resilience.ExponentialBackoffConfig]
    to avoid synchronized retry storms.

    Example:
    ```python
    from grelmicro.resilience import ConstantBackoffConfig, Retry

    policy = Retry(
        "wait_job",
        ConstantBackoffConfig(delay=1.0),
        on=NotReady,
        attempts=20,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    type: Annotated[
        Literal["constant"],
        Doc("Discriminator for the backoff Pydantic union."),
    ] = "constant"

    delay: Annotated[
        PositiveFloat,
        Doc("Fixed delay in seconds between retries."),
    ] = 1.0


class _ConstantStrategy:
    """Stateless constant backoff strategy."""

    __slots__ = ("_delay",)

    def __init__(self, config: ConstantBackoffConfig) -> None:
        self._delay = config.delay

    def delay(self, attempt: int) -> float:
        del attempt
        return self._delay
