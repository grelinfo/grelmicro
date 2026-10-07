"""Consecutive-count circuit breaker algorithm configuration."""

from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ImportString,
    PositiveInt,
    field_validator,
)
from pydantic_settings import NoDecode
from typing_extensions import Doc

from grelmicro._config import parse_csv_or_json
from grelmicro._duration import Duration
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

    ignore_exceptions: Annotated[
        tuple[ImportString[type[Exception]], ...],
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc(
            """
            Exceptions ignored by the breaker.

            Errors of these types do not count toward `error_threshold`.
            Accepts a single exception class, a tuple, or fully-qualified
            import strings such as `"builtins.ValueError"` or
            `"my_app.errors.PaymentError"` for YAML and env loading.

            Env vars accept comma-separated values or JSON arrays.
            """
        ),
    ] = ()

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

    @field_validator("ignore_exceptions", mode="before")
    @classmethod
    def _wrap_single(cls, value: Any) -> Any:  # noqa: ANN401
        """Wrap a single class into a one-tuple."""
        if isinstance(value, type):
            return (value,)
        return value
