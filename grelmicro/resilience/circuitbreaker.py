"""Circuit Breaker."""

import asyncio
import functools
import logging
import threading
from collections.abc import Callable
from contextvars import ContextVar
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

from grelmicro._config import (
    Reconfigurable,
    env_segment,
    parse_csv_or_json,
    resolve_config,
)
from grelmicro._types import LogLevel
from grelmicro.resilience._backends import get_circuit_breaker_backend
from grelmicro.resilience._protocol import CircuitBreakerBackend
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


class CircuitBreaker(Reconfigurable[CircuitBreakerConfig]):
    """Circuit Breaker.

    Implements the circuit breaker pattern. It watches calls to
    a protected service and blocks them when the service is
    failing, to avoid cascading errors.

    Supports live reconfiguration via
    `reconfigure(new_config)`.
    A swap takes effect on the next call. In-flight calls keep the
    config they entered with. The current state, counters, and
    `last_error` are kept. A new `log_level` is applied to the
    logger. See [Live reconfiguration](../architecture/reconfigure.md).
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
        backend: Annotated[
            CircuitBreakerBackend | str | None,
            Doc(
                """
                The circuit breaker backend that owns the lifespan
                and (in a future Redis-backed implementation) the
                shared state.

                Accepts a backend instance, the name of a registered
                backend (e.g. ``"analytics"``), or ``None`` to use the
                registered ``"default"`` backend.
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
        self._setup(name, config, backend)

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
        *,
        backend: Annotated[
            CircuitBreakerBackend | str | None,
            Doc("The circuit breaker backend."),
        ] = None,
    ) -> Self:
        """Construct a `CircuitBreaker` from a name and a pre-built `CircuitBreakerConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: CircuitBreakerConfig,
        backend: CircuitBreakerBackend | str | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._backend: CircuitBreakerBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )
        self._from_thread: _ThreadAdapter | None = None
        # Per-call snapshot stack. Each `__aenter__` pushes the config
        # captured at admission; `__aexit__` pops it and uses it for
        # success/error classification. ContextVar isolates concurrent
        # `async with cb:` calls across tasks and supports nesting in
        # the same task.
        self._enter_stack: ContextVar[tuple[CircuitBreakerConfig, ...]] = (
            ContextVar(f"_cb_enter_stack_{id(self)}", default=())
        )
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

    @property
    def backend(self) -> CircuitBreakerBackend:
        """Bound circuit breaker backend, resolved on each call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the registry is consulted on
        every access so that scoped overrides take effect.
        """
        if self._backend is not None:  # pragma: no cover
            return self._backend
        return get_circuit_breaker_backend(self._backend_name or "default")

    @property
    def from_thread(self) -> "_ThreadAdapter":
        """Sync adapter for use from a worker thread.

        Use it from a synchronous handler that the host framework runs
        in a worker thread. The adapter signals the intent explicitly
        so the async API stays the documented default.
        """
        if self._from_thread is None:
            self._from_thread = _ThreadAdapter(self)
        return self._from_thread

    async def __aenter__(self) -> Self:
        """Enter the circuit breaker context.

        Async is the primary API. It stays compatible with a future
        Redis-backed circuit breaker (issue #188) where ``__aenter__``
        will await backend I/O. Synchronous handlers go through
        ``cb.from_thread``.
        """
        backend = self.backend
        loop: asyncio.AbstractEventLoop | None = backend._loop  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        if loop is None:  # pragma: no cover
            backend._loop = asyncio.get_running_loop()  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        backend.register(self)
        config = self._config
        if not self._try_acquire_call(config):
            raise CircuitBreakerError(
                name=self.name,
                last_error_time=self._last_error_time,
                last_error=self._last_error,
            )
        self._enter_stack.set((*self._enter_stack.get(), config))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the circuit breaker context.

        Uses the same config snapshot captured at ``__aenter__`` so the
        success/error classification matches the admission decision.
        """
        stack = self._enter_stack.get()
        config = stack[-1]
        self._enter_stack.set(stack[:-1])

        self._release_call()

        if not exc_type or issubclass(exc_type, config.ignore_exceptions):
            self._on_success(config)

        elif isinstance(exc_value, Exception):
            self._on_error(config, exc_value)

        return None

    def _reset_state(self) -> None:
        """Clear runtime counters. Called by the backend on close."""
        self._state = CircuitBreakerState.CLOSED
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error = None
        self._last_error_time = None
        self._open_until_time = 0.0
        self._active_call_count = 0

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

    def restart(self) -> None:
        """Restart the circuit breaker, clearing all counts and resetting to CLOSED state."""
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error = None
        self._do_transition_to_state(
            self._config, CircuitBreakerState.CLOSED, _TransitionCause.RESTART
        )

    def transition_to_closed(self) -> None:
        """Transition the circuit breaker to CLOSED state."""
        self._do_transition_to_state(
            self._config, CircuitBreakerState.CLOSED, _TransitionCause.MANUAL
        )

    def transition_to_open(self, until: float | None = None) -> None:
        """Transition the circuit breaker to OPEN state."""
        self._do_transition_to_state(
            self._config,
            CircuitBreakerState.OPEN,
            _TransitionCause.MANUAL,
            open_until=until,
        )

    def transition_to_half_open(self) -> None:
        """Transition the circuit breaker to HALF_OPEN state."""
        self._do_transition_to_state(
            self._config, CircuitBreakerState.HALF_OPEN, _TransitionCause.MANUAL
        )

    def transition_to_forced_open(self) -> None:
        """Transition the circuit breaker to FORCED_OPEN state."""
        self._do_transition_to_state(
            self._config,
            CircuitBreakerState.FORCED_OPEN,
            _TransitionCause.MANUAL,
        )

    def transition_to_forced_closed(self) -> None:
        """Transition the circuit breaker to FORCED_CLOSED state."""
        self._do_transition_to_state(
            self._config,
            CircuitBreakerState.FORCED_CLOSED,
            _TransitionCause.MANUAL,
        )

    def _do_transition_to_state(
        self,
        config: CircuitBreakerConfig,
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
            + (config.reset_timeout if open_until is None else open_until)
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

    def _try_acquire_call(self, config: CircuitBreakerConfig) -> bool:
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
                config,
                CircuitBreakerState.HALF_OPEN,
                _TransitionCause.RESET_TIMEOUT,
            )

        if (
            self._state == CircuitBreakerState.HALF_OPEN
            and self._active_call_count < config.half_open_capacity
        ):
            self._active_call_count += 1
            return True
        return False

    def _release_call(self) -> None:
        """Release a call in the circuit breaker."""
        if self._active_call_count > 0:
            self._active_call_count -= 1

    def _on_error(self, config: CircuitBreakerConfig, error: Exception) -> None:
        """Record an error, update counts, and potentially transition state."""
        self._total_error_count += 1
        self._consecutive_error_count += 1
        self._consecutive_success_count = 0
        self._last_error = error
        self._last_error_time = datetime.now(UTC)

        if (
            self._state != CircuitBreakerState.OPEN
            and self._consecutive_error_count >= config.error_threshold
        ):
            self._do_transition_to_state(
                config,
                CircuitBreakerState.OPEN,
                _TransitionCause.ERROR_THRESHOLD,
            )

    def _on_success(self, config: CircuitBreakerConfig) -> None:
        """Record a success, update counts, and potentially transition state."""
        self._total_success_count += 1
        self._consecutive_error_count = 0
        self._consecutive_success_count += 1

        if (
            self._state == CircuitBreakerState.HALF_OPEN
            and self._consecutive_success_count >= config.success_threshold
        ):
            self._do_transition_to_state(
                config,
                CircuitBreakerState.CLOSED,
                _TransitionCause.SUCCESS_THRESHOLD,
            )

    async def _apply_reconfigure(
        self, new_config: CircuitBreakerConfig
    ) -> None:
        """Update the instance logger level to match `new_config.log_level`."""
        self._logger.setLevel(new_config.log_level)

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
    """Sync adapter for ``CircuitBreaker`` use from a worker thread.

    Stays forward-compatible with a future backend that performs real
    I/O: each entry/exit dispatches the corresponding internal helper
    on the loop captured by the backend. The admission-config snapshot
    stack is held in ``threading.local`` so concurrent worker threads
    do not collide.
    """

    def __init__(self, circuit_breaker: CircuitBreaker) -> None:
        """Initialize the adapter."""
        self._cb = circuit_breaker
        self._tls = threading.local()

    def __enter__(self) -> Self:
        """Enter the breaker context from a worker thread."""
        cb = self._cb
        backend = cb.backend
        loop = backend._loop  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        if loop is None:
            msg = (
                f"CircuitBreaker {cb.name!r} cannot be used from a worker "
                "thread before its backend is opened. Wrap startup with "
                "`async with grelmicro.lifespan():` or `async with backend:`."
            )
            raise RuntimeError(msg)
        config = cb._config  # noqa: SLF001
        if not asyncio.run_coroutine_threadsafe(
            _async_admit(cb, config), loop
        ).result():
            raise CircuitBreakerError(
                name=cb.name,
                last_error_time=cb.last_error_time,
                last_error=cb.last_error,
            )
        stack = getattr(self._tls, "stack", None)
        if stack is None:
            stack = []
            self._tls.stack = stack
        stack.append(config)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the breaker context from a worker thread."""
        cb = self._cb
        config = self._tls.stack.pop()
        loop = cb.backend._loop  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        asyncio.run_coroutine_threadsafe(
            _async_handle_exit(cb, config, exc_type, exc_value), loop
        ).result()
        return None


async def _async_admit(
    cb: CircuitBreaker, config: CircuitBreakerConfig
) -> bool:
    """Register and try to acquire a call. Runs on the backend loop.

    Both ``register`` and the counter mutation happen on the loop
    thread so worker threads never touch backend state directly.
    Forward-compatible: when a Redis-backed breaker arrives this
    coroutine will await backend I/O.
    """
    cb.backend.register(cb)
    return cb._try_acquire_call(config)  # noqa: SLF001


async def _async_handle_exit(
    cb: CircuitBreaker,
    config: CircuitBreakerConfig,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
) -> None:
    """Async wrapper around the breaker's sync exit path."""
    cb._release_call()  # noqa: SLF001
    if not exc_type or issubclass(exc_type, config.ignore_exceptions):
        cb._on_success(config)  # noqa: SLF001
    elif isinstance(exc_value, Exception):
        cb._on_error(config, exc_value)  # noqa: SLF001
