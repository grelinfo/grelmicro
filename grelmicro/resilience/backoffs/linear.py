"""Linear backoff configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class LinearBackoff(BaseModel, frozen=True, extra="forbid"):
    """Linear backoff: delay grows by ``base_delay`` each attempt.

    The delay before retry ``N`` is ``min(base_delay * N, max_delay)``.
    Use this for predictable progression when you have a rough idea
    of the recovery time and want a smooth ramp without exponential
    blow-up. Common for polling that escalates over time.

    For network and HTTP calls, prefer
    [`ExponentialBackoff`][grelmicro.resilience.ExponentialBackoff]
    to avoid synchronized retry storms.

    Example:
    ```python
    from grelmicro.resilience import LinearBackoff, Retry

    # 1s, 2s, 3s, 4s, ...
    policy = Retry(
        "ramp",
        LinearBackoff(base_delay=1.0, max_delay=10.0),
        on=ServiceError,
        attempts=5,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    kind: Annotated[
        Literal["linear"],
        Doc("Discriminator for the backoff Pydantic union."),
    ] = "linear"

    base_delay: Annotated[
        PositiveFloat,
        Doc("Increment in seconds added per attempt."),
    ] = 1.0

    max_delay: Annotated[
        PositiveFloat,
        Doc("Maximum delay in seconds. Caps the linear growth."),
    ] = 30.0


class _LinearStrategy:
    """Stateless linear backoff strategy."""

    __slots__ = ("_base", "_max")

    def __init__(self, config: LinearBackoff) -> None:
        self._base = config.base_delay
        self._max = config.max_delay

    def delay(self, attempt: int) -> float:
        return min(self._base * attempt, self._max)
