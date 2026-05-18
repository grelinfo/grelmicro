"""Consecutive-count circuit breaker algorithm configuration."""

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ImportString,
    PositiveFloat,
    PositiveInt,
    field_validator,
)
from pydantic_settings import NoDecode
from typing_extensions import Doc

from grelmicro._config import parse_csv_or_json
from grelmicro._types import LogLevel


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
    from grelmicro.resilience import CircuitBreaker, ConsecutiveCountConfig

    cb = CircuitBreaker.from_config(
        "payments",
        ConsecutiveCountConfig(error_threshold=5, reset_timeout=30.0),
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
        PositiveFloat,
        Doc(
            "Seconds the breaker stays `OPEN` before transitioning to `HALF_OPEN`."
        ),
    ] = 30.0

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
