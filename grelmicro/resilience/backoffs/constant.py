"""Constant backoff configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class ConstantBackoff(BaseModel, frozen=True, extra="forbid"):
    """Constant delay between retries.

    Use this for polling-style retries where you wait a fixed
    interval. For network and HTTP calls, prefer
    [`ExponentialBackoff`][grelmicro.resilience.ExponentialBackoff]
    to avoid synchronized retry storms.

    Example:
    ```python
    from grelmicro.resilience import ConstantBackoff, Retry

    policy = Retry(
        "wait_job",
        ConstantBackoff(delay=1.0),
        on=NotReady,
        attempts=20,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    kind: Annotated[
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

    def __init__(self, config: ConstantBackoff) -> None:
        self._delay = config.delay

    def delay(self, attempt: int) -> float:
        del attempt
        return self._delay
