"""Circuit Breaker."""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from inspect import iscoroutinefunction
from logging import getLogger
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro._config import Reconfigurable
from grelmicro.resilience.errors import CircuitBreakerError

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from pydantic import Discriminator

    from grelmicro._types import LogLevel
    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        CircuitBreakerSnapshot,
        CircuitBreakerStrategy,
    )
    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )

    CircuitBreakerConfig = Annotated[
        ConsecutiveCountConfig, Discriminator("kind")
    ]
    """Discriminated union of supported circuit-breaker algorithm configurations.

    Single-arm today. Future algorithms (failure-rate, slow-call) join
    the union via the `kind` discriminator without breaking existing
    serialized configs.
    """

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitBreakerMetrics",
    "CircuitBreakerState",
    "ConsecutiveCountConfig",
    "ErrorDetails",
]


def __getattr__(name: str) -> object:
    """PEP 562 lazy loader.

    Algorithm configs are imported on first access so that
    `from grelmicro.resilience.circuitbreaker import CircuitBreaker`
    does not pull in `consecutive_count.py` (or any future algorithm).
    """
    if name == "ConsecutiveCountConfig":
        from grelmicro.resilience.circuitbreaker.consecutive_count import (  # noqa: PLC0415
            ConsecutiveCountConfig,
        )

        return ConsecutiveCountConfig
    if name == "CircuitBreakerConfig":
        from pydantic import Discriminator  # noqa: PLC0415

        from grelmicro.resilience.circuitbreaker.consecutive_count import (  # noqa: PLC0415
            ConsecutiveCountConfig,
        )

        return Annotated[ConsecutiveCountConfig, Discriminator("kind")]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


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


class CircuitBreaker(Reconfigurable["CircuitBreakerConfig"]):
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

                Acts as the instance identity, exposed via the
                `name` property.
                """
            ),
        ],
        config: Annotated[
            CircuitBreakerConfig | None,
            Doc(
                """
                The algorithm configuration.

                Defaults to `ConsecutiveCountConfig()` with library
                defaults when omitted. Most callers should prefer the
                [`CircuitBreaker.consecutive_count`][grelmicro.resilience.CircuitBreaker.consecutive_count]
                factory classmethod to tweak the defaults. Pass a
                config directly when it is already assembled
                elsewhere, for example from YAML or a
                `pydantic-settings` tree.

                Today the discriminated union has a single arm:
                [`ConsecutiveCountConfig`][grelmicro.resilience.ConsecutiveCountConfig].
                Future algorithms (`failure_rate`, `slow_call`) join
                the union via the same `kind` discriminator.
                """
            ),
        ] = None,
        *,
        backend: Annotated[
            CircuitBreakerBackend | str | None,
            Doc(
                """
                The circuit breaker backend that owns the lifespan
                and (with a shared adapter) the cross-replica state.

                Accepts a backend instance, the name of a registered
                backend (e.g. ``"analytics"``), or ``None`` to fall
                back to the registered ``"default"`` Component or a
                process-global implicit memory adapter when no
                Component is registered.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the circuit breaker, defaulting the algorithm to consecutive-count."""
        if config is None:
            from grelmicro.resilience.circuitbreaker.consecutive_count import (  # noqa: PLC0415
                ConsecutiveCountConfig,
            )

            config = ConsecutiveCountConfig()
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

    @classmethod
    def consecutive_count(
        cls,
        name: Annotated[
            str,
            Doc("Name of the circuit breaker instance."),
        ],
        *,
        ignore_exceptions: Annotated[
            type[Exception] | str | tuple[type[Exception] | str, ...] | None,
            Doc("Exceptions ignored by the breaker."),
        ] = None,
        error_threshold: Annotated[
            PositiveInt | None,
            Doc("Consecutive errors before the breaker opens. Default: 5."),
        ] = None,
        success_threshold: Annotated[
            PositiveInt | None,
            Doc(
                "Consecutive successes in `HALF_OPEN` before the breaker closes. Default: 2."
            ),
        ] = None,
        reset_timeout: Annotated[
            PositiveFloat | None,
            Doc(
                "Seconds the breaker stays `OPEN` before transitioning"
                " to `HALF_OPEN`. Default: 30.0."
            ),
        ] = None,
        half_open_capacity: Annotated[
            PositiveInt | None,
            Doc(
                "Maximum concurrent calls allowed in the `HALF_OPEN` state. Default: 1."
            ),
        ] = None,
        log_level: Annotated[
            LogLevel | None,
            Doc("Logging level for state-change messages. Default: `WARNING`."),
        ] = None,
        backend: Annotated[
            CircuitBreakerBackend | str | None,
            Doc("The circuit breaker backend."),
        ] = None,
    ) -> Self:
        """Construct a `CircuitBreaker` running the consecutive-count algorithm.

        Sibling of [`from_config`][grelmicro.resilience.CircuitBreaker.from_config]
        and the bare constructor: the bare constructor reads env vars
        for unset fields, this factory does not.
        """
        from grelmicro.resilience.circuitbreaker.consecutive_count import (  # noqa: PLC0415
            ConsecutiveCountConfig,
        )

        provided: dict[str, Any] = {
            "ignore_exceptions": ignore_exceptions,
            "error_threshold": error_threshold,
            "success_threshold": success_threshold,
            "reset_timeout": reset_timeout,
            "half_open_capacity": half_open_capacity,
            "log_level": log_level,
        }
        config = ConsecutiveCountConfig.model_validate(
            {k: v for k, v in provided.items() if v is not None}
        )
        return cls.from_config(name, config, backend=backend)

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
        # Per-call config stack for in-flight reconfigure correctness.
        # Each `__aenter__` pushes the config captured at admission;
        # `__aexit__` pops it and uses it for `ignore_exceptions`
        # classification. ContextVar isolates concurrent `async with cb:`
        # calls across tasks and supports nesting in the same task.
        self._enter_stack: ContextVar[tuple[CircuitBreakerConfig, ...]] = (
            ContextVar(f"_cb_enter_stack_{id(self)}", default=())
        )
        # Bound strategy snapshot. Rebound lazily on first use after a
        # backend change or reconfigure.
        self._state = _State(config=config, strategy=None)
        # Local snapshot cache for `cb.state` and `cb.metrics()`.
        # Refreshed from strategy returns.
        self._cached_state = CircuitBreakerState.CLOSED
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        # Per-replica counters and concurrency.
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error: Exception | None = None
        self._last_error_time: datetime | None = None
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

        Resolution order:
        1. An explicit `backend=` passed at construction wins.
        2. The active `Grelmicro` app is consulted via
           `Grelmicro.current()` so that `micro.override(...)` blocks
           take effect.
        3. If no app is active or no `CircuitBreakers` Component is
           registered, fall back to a process-global implicit
           `MemoryCircuitBreakerAdapter`. Lets `CircuitBreaker("name")`
           work without any Grelmicro setup for the per-replica
           recommended default.
        """
        if self._backend is not None:  # pragma: no cover
            return self._backend
        from grelmicro._app import (  # noqa: PLC0415
            ComponentNotRegisteredError,
            NoActiveAppError,
        )

        try:
            component = Grelmicro.current().get(
                "circuitbreaker", self._backend_name or "default"
            )
        except (NoActiveAppError, ComponentNotRegisteredError):
            return _implicit_backend()
        return component.backend

    @property
    def from_thread(self) -> _ThreadAdapter:
        """Sync adapter for use from a worker thread.

        Use it from a synchronous handler that the host framework runs
        in a worker thread. The adapter signals the intent explicitly
        so the async API stays the documented default.
        """
        if self._from_thread is None:
            self._from_thread = _ThreadAdapter(self)
        return self._from_thread

    def _resolve_strategy(self, state: _State) -> CircuitBreakerStrategy:
        """Bind the published config to the current backend and republish.

        Strategy parameters (thresholds, capacities) reflect the
        currently published config. Calls that entered before a
        ``reconfigure`` keep their entry config in ``_enter_stack`` and
        use it for ``ignore_exceptions`` classification on exit, so the
        admission decision and the outcome classification stay
        consistent for an in-flight call. Threshold checks happen
        inside the strategy and use the freshly bound values.
        """
        strategy = self.backend.bind(name=self._name, config=state.config)
        self._state = _State(config=state.config, strategy=strategy)
        return strategy

    async def __aenter__(self) -> Self:
        """Enter the circuit breaker context.

        Async is the primary API. Synchronous handlers go through
        ``cb.from_thread``.
        """
        backend = self.backend
        loop: asyncio.AbstractEventLoop | None = backend._loop  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        if loop is None:  # pragma: no cover
            backend._loop = asyncio.get_running_loop()  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        state = self._state
        strategy = state.strategy or self._resolve_strategy(state)
        if not await strategy.try_acquire():
            self._apply_snapshot(await strategy.get_snapshot())
            raise CircuitBreakerError(
                name=self.name,
                last_error_time=self._last_error_time,
                last_error=self._last_error,
            )
        self._active_call_count += 1
        self._enter_stack.set((*self._enter_stack.get(), state.config))
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
        state = self._state
        strategy = state.strategy or self._resolve_strategy(state)
        if not exc_type or issubclass(exc_type, config.ignore_exceptions):
            snapshot = await strategy.record_outcome(success=True)
            self._total_success_count += 1
        elif isinstance(exc_value, Exception):
            snapshot = await strategy.record_outcome(success=False)
            self._total_error_count += 1
            self._last_error = exc_value
            self._last_error_time = datetime.now(UTC)
        else:  # pragma: no cover
            return None
        self._apply_snapshot(snapshot)
        return None

    def _apply_snapshot(self, snapshot: CircuitBreakerSnapshot) -> None:
        """Refresh local cache from the strategy snapshot and log transitions."""
        previous = self._cached_state
        new = snapshot.state
        self._cached_state = new
        self._consecutive_error_count = snapshot.consecutive_error_count
        self._consecutive_success_count = snapshot.consecutive_success_count
        if previous != new:
            self._log_transition(new, _derive_cause(previous, new))

    def _log_transition(
        self,
        state: CircuitBreakerState,
        cause: _TransitionCause,
    ) -> None:
        """Emit the state-change log line."""
        self._logger.log(
            logging.ERROR
            if state == CircuitBreakerState.OPEN
            else logging.INFO,
            "Circuit breaker '%s' state changed to %s [cause: %s]",
            self._name,
            state,
            cause,
        )

    @property
    def name(self) -> str:
        """Return the name of the circuit breaker."""
        return self._name

    @property
    def state(self) -> CircuitBreakerState:
        """Return the current cached state of the circuit breaker."""
        return self._cached_state

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
        return CircuitBreakerMetrics(
            name=self._name,
            state=self._cached_state,
            active_calls=self._active_call_count,
            total_error_count=self._total_error_count,
            total_success_count=self._total_success_count,
            consecutive_error_count=self._consecutive_error_count,
            consecutive_success_count=self._consecutive_success_count,
            last_error=self._map_last_error(),
        )

    async def restart(self) -> None:
        """Restart the breaker, clearing all counts and resetting to CLOSED state."""
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error = None
        self._last_error_time = None
        await self._transition(
            CircuitBreakerState.CLOSED, _TransitionCause.RESTART
        )

    async def transition_to_closed(self) -> None:
        """Transition the circuit breaker to CLOSED state."""
        await self._transition(
            CircuitBreakerState.CLOSED, _TransitionCause.MANUAL
        )

    async def transition_to_open(self, until: float | None = None) -> None:
        """Transition the circuit breaker to OPEN state."""
        await self._transition(
            CircuitBreakerState.OPEN,
            _TransitionCause.MANUAL,
            cool_down=until,
        )

    async def transition_to_half_open(self) -> None:
        """Transition the circuit breaker to HALF_OPEN state."""
        await self._transition(
            CircuitBreakerState.HALF_OPEN, _TransitionCause.MANUAL
        )

    async def transition_to_forced_open(self) -> None:
        """Transition the circuit breaker to FORCED_OPEN state."""
        await self._transition(
            CircuitBreakerState.FORCED_OPEN, _TransitionCause.MANUAL
        )

    async def transition_to_forced_closed(self) -> None:
        """Transition the circuit breaker to FORCED_CLOSED state."""
        await self._transition(
            CircuitBreakerState.FORCED_CLOSED, _TransitionCause.MANUAL
        )

    async def _transition(
        self,
        desired: CircuitBreakerState,
        cause: _TransitionCause,
        cool_down: float | None = None,
    ) -> None:
        """Forward the transition to the strategy and refresh local cache."""
        state = self._state
        strategy = state.strategy or self._resolve_strategy(state)
        await strategy.transition(desired=desired, cool_down=cool_down)
        self._cached_state = desired
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._log_transition(desired, cause)

    def _release_call(self) -> None:
        """Release a call in the circuit breaker."""
        if self._active_call_count > 0:
            self._active_call_count -= 1

    async def _apply_reconfigure(
        self, new_config: CircuitBreakerConfig
    ) -> None:
        """Rebind the strategy with the new config and update the logger level."""
        self._logger.setLevel(new_config.log_level)
        # Clear the cached strategy. The next call rebinds it through
        # `_resolve_strategy` with the freshly published config.
        self._state = _State(config=new_config, strategy=None)

    def _map_last_error(self) -> ErrorDetails | None:
        """Map the last error to ErrorDetails."""
        if not self._last_error or not self._last_error_time:
            return None

        return ErrorDetails(
            time=self._last_error_time,
            type=type(self._last_error).__name__,
            msg=str(self._last_error),
        )


@dataclass(frozen=True, slots=True)
class _State:
    """Read-side snapshot bundling the published config with its bound strategy."""

    config: CircuitBreakerConfig
    strategy: CircuitBreakerStrategy | None


def _derive_cause(
    previous: CircuitBreakerState,
    new: CircuitBreakerState,
) -> _TransitionCause:
    """Infer the cause of an automatic transition from the direction.

    Manual transitions go through ``_transition`` with an explicit
    cause. This helper only covers transitions surfaced via a strategy
    snapshot.
    """
    if new == CircuitBreakerState.OPEN:
        return _TransitionCause.ERROR_THRESHOLD
    if (
        new == CircuitBreakerState.HALF_OPEN
        and previous == CircuitBreakerState.OPEN
    ):
        return _TransitionCause.RESET_TIMEOUT
    if new == CircuitBreakerState.CLOSED:
        return _TransitionCause.SUCCESS_THRESHOLD
    return _TransitionCause.MANUAL


class _ThreadAdapter:
    """Sync adapter for ``CircuitBreaker`` use from a worker thread.

    Each entry/exit dispatches the corresponding internal helper on the
    loop captured by the backend. The admission-config snapshot stack
    is held in ``threading.local`` so concurrent worker threads do not
    collide.
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
        config = cb._state.config  # noqa: SLF001
        if not asyncio.run_coroutine_threadsafe(
            _async_admit(cb), loop
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


async def _async_admit(cb: CircuitBreaker) -> bool:
    """Try to acquire a call. Runs on the backend loop.

    Mutates per-replica counters from the loop thread so worker threads
    never touch breaker state directly.
    """
    state = cb._state  # noqa: SLF001
    strategy = state.strategy or cb._resolve_strategy(state)  # noqa: SLF001
    if await strategy.try_acquire():
        cb._active_call_count += 1  # noqa: SLF001
        return True
    cb._apply_snapshot(await strategy.get_snapshot())  # noqa: SLF001
    return False


async def _async_handle_exit(
    cb: CircuitBreaker,
    config: CircuitBreakerConfig,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
) -> None:
    """Async wrapper around the breaker's exit path. Runs on the backend loop."""
    cb._release_call()  # noqa: SLF001
    state = cb._state  # noqa: SLF001
    strategy = state.strategy or cb._resolve_strategy(state)  # noqa: SLF001
    if not exc_type or issubclass(exc_type, config.ignore_exceptions):
        snapshot = await strategy.record_outcome(success=True)
        cb._total_success_count += 1  # noqa: SLF001
    elif isinstance(exc_value, Exception):
        snapshot = await strategy.record_outcome(success=False)
        cb._total_error_count += 1  # noqa: SLF001
        cb._last_error = exc_value  # noqa: SLF001
        cb._last_error_time = datetime.now(UTC)  # noqa: SLF001
    else:  # pragma: no cover
        return
    cb._apply_snapshot(snapshot)  # noqa: SLF001


_IMPLICIT_BACKEND: CircuitBreakerBackend | None = None


def _implicit_backend() -> CircuitBreakerBackend:
    """Return the process-global implicit memory adapter.

    Built lazily on first access so that importing
    `grelmicro.resilience.circuitbreaker` does not load the memory
    adapter. Used when no `CircuitBreakers` Component is registered,
    so `CircuitBreaker("name")` works without any `Grelmicro` setup
    for the per-replica recommended default.
    """
    global _IMPLICIT_BACKEND  # noqa: PLW0603
    if _IMPLICIT_BACKEND is None:
        from grelmicro.resilience.circuitbreaker.memory import (  # noqa: PLC0415
            MemoryCircuitBreakerAdapter,
        )

        _IMPLICIT_BACKEND = MemoryCircuitBreakerAdapter()
    return _IMPLICIT_BACKEND
