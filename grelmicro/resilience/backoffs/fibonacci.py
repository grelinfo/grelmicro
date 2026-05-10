"""Fibonacci backoff configuration."""

from typing import Annotated, Literal

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class FibonacciBackoff(BaseModel, frozen=True, extra="forbid"):
    """Fibonacci backoff: delays follow the Fibonacci sequence scaled by ``base_delay``.

    The delay before retry ``N`` is ``min(base_delay * fib(N), max_delay)``,
    where ``fib(1) = 1``, ``fib(2) = 1``, ``fib(3) = 2``, ``fib(4) = 3``, ...

    Sits between linear and exponential. Slower than exponential but
    eventually outpaces linear. Used historically in TCP and some
    retry libraries (Tenacity, backon, backoff).

    For most retries, prefer
    [`ExponentialBackoff`][grelmicro.resilience.ExponentialBackoff].
    Reach for Fibonacci when exponential's growth is too aggressive
    and linear's is too slow.

    Example:
    ```python
    from grelmicro.resilience import FibonacciBackoff, Retry

    # 1s, 1s, 2s, 3s, 5s, 8s, ...
    policy = Retry(
        "deferred",
        FibonacciBackoff(base_delay=1.0, max_delay=60.0),
        on=ServiceError,
        attempts=8,
    )
    ```

    Read more in the [Retry](../resilience/retry.md) docs.
    """

    type: Annotated[
        Literal["fibonacci"],
        Doc("Discriminator for the backoff Pydantic union."),
    ] = "fibonacci"

    base_delay: Annotated[
        PositiveFloat,
        Doc("Multiplier in seconds applied to each Fibonacci term."),
    ] = 1.0

    max_delay: Annotated[
        PositiveFloat,
        Doc("Maximum delay in seconds. Caps the Fibonacci growth."),
    ] = 30.0


class _FibonacciStrategy:
    """Stateful Fibonacci backoff strategy.

    Holds the two previous Fibonacci terms so each ``delay`` call
    is O(1). One strategy is built per retry loop.
    """

    __slots__ = ("_attempt", "_base", "_max", "_prev", "_prev_prev")

    def __init__(self, config: FibonacciBackoff) -> None:
        self._base = config.base_delay
        self._max = config.max_delay
        self._prev_prev = 0
        self._prev = 1
        self._attempt = 0

    def delay(self, attempt: int) -> float:
        # Advance the Fibonacci sequence to ``attempt``. Strategies are
        # called with monotonically increasing attempt numbers, but
        # guard against duplicate or out-of-order calls.
        while self._attempt < attempt:
            self._prev_prev, self._prev = (
                self._prev,
                self._prev_prev + self._prev,
            )
            self._attempt += 1
        return min(self._base * self._prev_prev, self._max)
