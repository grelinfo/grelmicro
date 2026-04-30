"""Circuit Breaker."""

import functools
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from inspect import iscoroutinefunction
from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import (
    Annotated,
    Any,
    Self,
)

from anyio import from_thread
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

from grelmicro._config import env_segment, parse_csv_or_json, resolve_config
from grelmicro._types import LogLevel
from grelmicro.resilience.errors import CircuitBreakerError

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerState",
    "ErrorDetails",
]


class _TransitionCause(StrEnum):
    """Cause of a circuit breaker state transition."""

    ERROR_THRESHOLD = "error_threshold"
    """Transition due to reaching the error threshold."""
    SUCCESS_THRESHOLD = "success_threshold"
    """Transition due to reaching the success threshold."""
    RESET_TIMEOUT = "reset_timeout"
    """Transition due to timeout after the circuit was open."""
    MANUAL = "manual"
    """Transition due to manual intervention."""
    RESTART = "restart"
    """Transition due to circuit breaker restart."""


class CircuitBreakerState(StrEnum):
    """Circuit breaker state.

    State machine diagram:
    ```
    ┌────────┐ errors >= threshold  ┌────────┐
    │ CLOSED │────────────────────> │  OPEN  │ <─┐
    └────────┘                      └────────┘   │
        ▲                       timeout │        │ errors >= threshold
        │                               ▼        │
        │                         ┌───────────┐  │
        └─────────────────────────│ HALF_OPEN │──┘
          success >= threshold    └───────────┘
    ```
    """

    CLOSED = "CLOSED"
    """Circuit is closed, calls are allowed."""
    OPEN = "OPEN"
    """Circuit is open, calls are not allowed."""
    HALF_OPEN = "HALF_OPEN"
    """Circuit is half-open, calls are limited."""
    FORCED_OPEN = "FORCED_OPEN"
    """Circuit is open for an indefinite time, calls are not allowed."""
    FORCED_CLOSED = "FORCED_CLOSED"
    """Circuit is forced closed for an indefinite time, calls are allowed."""


class ErrorDetails(BaseModel, frozen=True, extra="forbid"):
    """Details about an error recorded by the circuit breaker."""

    time: datetime
    type: str
    msg: str


class CircuitBreakerMetrics(BaseModel, frozen=True, extra="forbid"):
    """Circuit breaker metrics."""

    name: str
    state: CircuitBreakerState
    active_calls: int
    total_error_count: int
    total_success_count: int
    consecutive_error_count: int
    consecutive_success_count: int
    last_error: ErrorDetails | None


class CircuitBreakerConfig(BaseModel, frozen=True, extra="forbid"):
    """Circuit Breaker Config."""

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


class CircuitBreaker:
    """Circuit Breaker.

    Implements the circuit breaker pattern. It watches calls to
    a protected service and blocks them when the service is
    failing, to avoid cascading errors.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                """
                Name of the circuit breaker instance.

                Acts as the instance identity. Used as the env var
                prefix and exposed via the `name` property.
                """
            ),
        ],
        *,
        ignore_exceptions: Annotated[
            type[Exception] | str | tuple[type[Exception] | str, ...] | None,
            Doc(
                """
                Exceptions ignored by the breaker.

                Errors of these types do not count toward `error_threshold`.
                Accepts a single exception class, a tuple, or fully-qualified
                import strings such as `"builtins.ValueError"` or
                `"my_app.errors.PaymentError"`. When unset and env reads are enabled (see `read_env`
                and `GREL_CONFIG_FROM_ENV`), resolves from the env path or
                falls back to the `CircuitBreakerConfig` default (empty tuple).
                """
            ),
        ] = None,
        error_threshold: Annotated[
            PositiveInt | None,
            Doc(
                """
                Consecutive errors before the breaker opens.

                Default: 5. When unset and env reads are enabled (see `read_env`
                and `GREL_CONFIG_FROM_ENV`), resolves from
                `GREL_CIRCUIT_BREAKER_{NAME_UPPER}_ERROR_THRESHOLD` if
                present, otherwise falls back to the
                `CircuitBreakerConfig` default.
                """
            ),
        ] = None,
        success_threshold: Annotated[
            PositiveInt | None,
            Doc(
                """
                Consecutive successes in `HALF_OPEN` state before the breaker closes.

                Default: 2.
                """
            ),
        ] = None,
        reset_timeout: Annotated[
            PositiveFloat | None,
            Doc(
                """
                Seconds the breaker stays `OPEN` before transitioning to `HALF_OPEN`.

                Default: 30.0.
                """
            ),
        ] = None,
        half_open_capacity: Annotated[
            PositiveInt | None,
            Doc(
                """
                Maximum concurrent calls allowed in the `HALF_OPEN` state.

                Default: 1.
                """
            ),
        ] = None,
        log_level: Annotated[
            LogLevel | None,
            Doc(
                """
                Logging level for state-change messages.

                Default: `WARNING`.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_CIRCUIT_BREAKER_{NAME_UPPER}_`.
                """
            ),
        ] = None,
        read_env: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_CONFIG_FROM_ENV`` flag. Pass True or False to
                override the flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the circuit breaker."""
        config = resolve_config(
            CircuitBreakerConfig,
            explicit=None,
            kwargs={
                "ignore_exceptions": ignore_exceptions,
                "error_threshold": error_threshold,
                "success_threshold": success_threshold,
                "reset_timeout": reset_timeout,
                "half_open_capacity": half_open_capacity,
                "log_level": log_level,
            },
            env_prefix=env_prefix
            or f"GREL_CIRCUIT_BREAKER_{env_segment(name)}_",
            read_env=read_env,
        )
        self._setup(name, config)

    @classmethod
    def from_config(
        cls,
        name: Annotated[
            str,
            Doc("Name of the circuit breaker instance."),
        ],
        config: Annotated[
            CircuitBreakerConfig,
            Doc(
                """
                The pre-built circuit breaker configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree. The environment path is
                bypassed and the config is used as-is.
                """
            ),
        ],
    ) -> Self:
        """Construct a `CircuitBreaker` from a name and a pre-built `CircuitBreakerConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config)  # noqa: SLF001
        return instance

    def _setup(self, name: str, config: CircuitBreakerConfig) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._state = CircuitBreakerState.CLOSED
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error: Exception | None = None
        self._last_error_time: datetime | None = None
        self._open_until_time = 0.0
        self._active_call_count = 0
        self._logger = getLogger(f"grelmicro.circuitbreaker.{name}")
        self._logger.setLevel(config.log_level)
        self._from_thread: _ThreadAdapter | None = None

    def __call__(
        self, func: Callable[..., Any] | None = None
    ) -> Callable[..., Any]:
        """Return a decorator that protects a function with the circuit breaker."""
        if func is None:
            return self.__call__

        if iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                async with self:
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            with self.from_thread:
                return func(*args, **kwargs)

        return sync_wrapper

    async def __aenter__(self) -> Self:
        """Circuit breaker context manager."""
        if not await self._try_acquire_call():
            raise CircuitBreakerError(
                name=self.name,
                last_error_time=self._last_error_time,
                last_error=self._last_error,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the context manager."""
        await self._release_call()

        if not exc_type or issubclass(exc_type, self._config.ignore_exceptions):
            await self._on_success()

        elif isinstance(exc_value, Exception):
            await self._on_error(exc_value)

        return None

    @property
    def from_thread(self) -> "_ThreadAdapter":
        """Return the lock adapter for worker thread."""
        if self._from_thread is None:
            self._from_thread = _ThreadAdapter(self)
        return self._from_thread

    @property
    def name(self) -> str:
        """Return the name of the circuit breaker."""
        return self._name

    @property
    def state(self) -> CircuitBreakerState:
        """Return the current state of the circuit breaker."""
        return self._state

    @property
    def last_error(self) -> Exception | None:
        """Return the last error recorded by the circuit breaker."""
        return self._last_error

    @property
    def last_error_time(self) -> datetime | None:
        """Return the time of the last error recorded by the circuit breaker."""
        return self._last_error_time

    @property
    def config(self) -> CircuitBreakerConfig:
        """Return the circuit breaker configuration."""
        return self._config

    def metrics(self) -> CircuitBreakerMetrics:
        """Return current metrics for this circuit breaker."""
        last_error = self._map_last_error()

        return CircuitBreakerMetrics(
            name=self._name,
            state=self._state,
            active_calls=self._active_call_count,
            total_error_count=self._total_error_count,
            total_success_count=self._total_success_count,
            consecutive_error_count=self._consecutive_error_count,
            consecutive_success_count=self._consecutive_success_count,
            last_error=last_error,
        )

    async def restart(self) -> None:
        """Restart the circuit breaker, clearing all counts and resetting to CLOSED state."""
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error = None
        self._do_transition_to_state(
            CircuitBreakerState.CLOSED, _TransitionCause.RESTART
        )

    async def transition_to_closed(self) -> None:
        """Transition the circuit breaker to CLOSED state."""
        self._do_transition_to_state(
            CircuitBreakerState.CLOSED, _TransitionCause.MANUAL
        )

    async def transition_to_open(self, until: float | None = None) -> None:
        """Transition the circuit breaker to OPEN state."""
        self._do_transition_to_state(
            CircuitBreakerState.OPEN, _TransitionCause.MANUAL, open_until=until
        )

    async def transition_to_half_open(self) -> None:
        """Transition the circuit breaker to HALF_OPEN state."""
        self._do_transition_to_state(
            CircuitBreakerState.HALF_OPEN, _TransitionCause.MANUAL
        )

    async def transition_to_forced_open(self) -> None:
        """Transition the circuit breaker to FORCED_OPEN state."""
        self._do_transition_to_state(
            CircuitBreakerState.FORCED_OPEN, _TransitionCause.MANUAL
        )

    async def transition_to_forced_closed(self) -> None:
        """Transition the circuit breaker to FORCED_CLOSED state."""
        self._do_transition_to_state(
            CircuitBreakerState.FORCED_CLOSED, _TransitionCause.MANUAL
        )

    def _do_transition_to_state(
        self,
        state: CircuitBreakerState,
        cause: _TransitionCause,
        open_until: float | None = None,
    ) -> None:
        """Transition to a new state and reset consecutive counts."""
        self._state = state
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0

        self._open_until_time = (
            monotonic()
            + (self._config.reset_timeout if open_until is None else open_until)
            if state == CircuitBreakerState.OPEN
            else 0
        )

        self._logger.log(
            logging.ERROR
            if state == CircuitBreakerState.OPEN
            else logging.INFO,
            "Circuit breaker '%s' state changed to %s [cause: %s]",
            self.name,
            state,
            cause,
        )

    async def _try_acquire_call(self) -> bool:
        """Attempt to acquire a call in the circuit breaker."""
        if self._state in (
            CircuitBreakerState.CLOSED,
            CircuitBreakerState.FORCED_CLOSED,
        ):
            self._active_call_count += 1
            return True

        if (
            self._state == CircuitBreakerState.OPEN
            and monotonic() >= self._open_until_time
        ):
            self._do_transition_to_state(
                CircuitBreakerState.HALF_OPEN, _TransitionCause.RESET_TIMEOUT
            )

        if (
            self._state == CircuitBreakerState.HALF_OPEN
            and self._active_call_count < self._config.half_open_capacity
        ):
            self._active_call_count += 1
            return True
        return False

    async def _release_call(self) -> None:
        """Release a call in the circuit breaker."""
        if self._active_call_count > 0:
            self._active_call_count -= 1

    async def _on_error(self, error: Exception) -> None:
        """Record an error, update counts, and potentially transition state."""
        self._total_error_count += 1
        self._consecutive_error_count += 1
        self._consecutive_success_count = 0
        self._last_error = error
        self._last_error_time = datetime.now(UTC)

        if (
            self._state != CircuitBreakerState.OPEN
            and self._consecutive_error_count >= self._config.error_threshold
        ):
            self._do_transition_to_state(
                CircuitBreakerState.OPEN, _TransitionCause.ERROR_THRESHOLD
            )

    async def _on_success(self) -> None:
        """Record a success, update counts, and potentially transition state."""
        self._total_success_count += 1
        self._consecutive_error_count = 0
        self._consecutive_success_count += 1

        if (
            self._state == CircuitBreakerState.HALF_OPEN
            and self._consecutive_success_count
            >= self._config.success_threshold
        ):
            self._do_transition_to_state(
                CircuitBreakerState.CLOSED, _TransitionCause.SUCCESS_THRESHOLD
            )

    def _map_last_error(self) -> ErrorDetails | None:
        """Map the last error to ErrorDetails."""
        if not self._last_error or not self._last_error_time:
            return None

        return ErrorDetails(
            time=self._last_error_time,
            type=type(self._last_error).__name__,
            msg=str(self._last_error),
        )


class _ThreadAdapter:
    """Adapter for using CircuitBreaker in a thread context."""

    def __init__(self, circuit_breaker: CircuitBreaker) -> None:
        """Initialize the adapter with a CircuitBreaker instance."""
        self._cb = circuit_breaker

    def __enter__(self) -> Self:
        """Enter the context manager, acquiring the circuit breaker lock."""
        if not from_thread.run(self._cb._try_acquire_call):  # noqa: SLF001
            raise CircuitBreakerError(
                name=self._cb.name,
                last_error_time=self._cb.last_error_time,
                last_error=self._cb.last_error,
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the context manager, releasing the circuit breaker lock."""
        from_thread.run(self._cb._release_call)  # noqa: SLF001

        if not exc_type or issubclass(
            exc_type,
            self._cb._config.ignore_exceptions,  # noqa: SLF001
        ):
            from_thread.run(self._cb._on_success)  # noqa: SLF001

        elif isinstance(exc_value, Exception):
            from_thread.run(self._cb._on_error, exc_value)  # noqa: SLF001

        return None

    def restart(self) -> None:
        """Restart the circuit breaker, clearing all counts and resetting to CLOSED state."""
        from_thread.run(self._cb.restart)

    def transition_to_closed(self) -> None:
        """Transition the circuit breaker to CLOSED state."""
        from_thread.run(self._cb.transition_to_closed)

    def transition_to_open(self, until: float | None = None) -> None:
        """Transition the circuit breaker to OPEN state."""
        from_thread.run(self._cb.transition_to_open, until)

    def transition_to_half_open(self) -> None:
        """Transition the circuit breaker to HALF_OPEN state."""
        from_thread.run(self._cb.transition_to_half_open)

    def transition_to_forced_open(self) -> None:
        """Transition the circuit breaker to FORCED_OPEN state."""
        from_thread.run(self._cb.transition_to_forced_open)

    def transition_to_forced_closed(self) -> None:
        """Transition the circuit breaker to FORCED_CLOSED state."""
        from_thread.run(self._cb.transition_to_forced_closed)
