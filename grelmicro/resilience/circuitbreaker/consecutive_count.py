"""Consecutive-count circuit breaker algorithm configuration."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, PositiveInt
from typing_extensions import Doc

from grelmicro._duration import Duration
from grelmicro.resilience._match import Match
from grelmicro.resilience._when import OutcomeFilter
from grelmicro.types import LogLevel


class ConsecutiveCountConfig(BaseModel, frozen=True, extra="forbid"):
    """Consecutive-count circuit breaker algorithm.

    Opens after `error_threshold` consecutive failures. Closes from
    `HALF_OPEN` after `success_threshold` consecutive successes. A
    single success in `CLOSED` resets the running error count.

    Use this when failures cluster, for example transient downstream
    outages where the first N errors in a row are a strong signal. For
    failure-rate or slow-call detection, plug in a future algorithm
    config through the same `kind` discriminator.

    Example:
    ```python
    from datetime import timedelta

    from grelmicro.resilience import CircuitBreaker, ConsecutiveCountConfig

    cb = CircuitBreaker.from_config(
        "payments",
        ConsecutiveCountConfig(
            error_threshold=5, reset_timeout=timedelta(seconds=30)
        ),
    )
    ```

    Read more in the [Circuit Breaker](../resilience/circuit-breaker.md) docs.
    """

    kind: Annotated[
        Literal["consecutive_count"],
        Doc("Discriminator for the algorithm Pydantic union."),
    ] = "consecutive_count"

    when: Annotated[
        OutcomeFilter,
        Doc(
            """
            Outcome filter naming the errors that count as failures.

            Pass a [`Match`][grelmicro.resilience.Match] or a shorthand:
            an exception class, a tuple of classes, or a predicate on the
            exception. A raised exception it does not match counts as a
            success. The breaker sees raised exceptions only, so a
            returned value always counts as a success and a
            `Match.result(...)` arm never matches. A predicate that
            raises reads as no match, so that error counts as a success.
            Default: every `Exception` counts as a failure.

            From text, such as an environment variable, it reads
            fully-qualified class names as comma-separated values or a
            JSON array, such as `"httpx.HTTPError"`.
            """
        ),
    ] = Match.exception(Exception)

    error_threshold: Annotated[
        PositiveInt,
        Doc("Consecutive errors before the breaker opens."),
    ] = 5

    success_threshold: Annotated[
        PositiveInt,
        Doc(
            "Consecutive successes in `HALF_OPEN` state before the breaker closes."
        ),
    ] = 2

    reset_timeout: Annotated[
        Duration,
        Doc(
            """
            How long the breaker stays `OPEN` before transitioning to
            `HALF_OPEN`, in whole seconds or as a `timedelta`.

            A float is refused. From text, such as an environment
            variable, it reads whole seconds (`"30"`) or an ISO 8601
            duration (`"PT0.5S"`).
            """
        ),
    ] = timedelta(seconds=30)

    half_open_capacity: Annotated[
        PositiveInt,
        Doc("Maximum concurrent calls allowed in the `HALF_OPEN` state."),
    ] = 1

    log_level: Annotated[
        LogLevel,
        Doc("Logging level for state-change messages."),
    ] = "WARNING"
