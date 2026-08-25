"""Circuit Breaker."""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from inspect import iscoroutinefunction
from logging import getLogger
from typing import TYPE_CHECKING, Annotated, Any, Self, overload

from typing_extensions import Doc

from grelmicro._app import resolve_ambient
from grelmicro._async import (
    on_backend_loop,
    raise_backend_not_open,
    raise_event_loop_deadlock,
)
from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    env_prefixes,
    resolve_config,
)
from grelmicro._wrapping import refuse_registered
from grelmicro.clock import monotonic
from grelmicro.errors import OutOfContextError
from grelmicro.metrics import _emit
from grelmicro.resilience.errors import CircuitBreakerError

_STATE_CODE = {
    "CLOSED": 0,
    "OPEN": 1,
    "HALF_OPEN": 2,
    "FORCED_OPEN": 3,
    "FORCED_CLOSED": 4,
}
"""Numeric codes for the `grelmicro.circuit_breaker.state` gauge."""

_KEYED_MAXSIZE = 1024
"""Per-key circuits a breaker keeps resident before evicting."""

_KEYED_RESIDENCY = 300.0
"""Seconds a per-key circuit is immune from eviction after its last call."""

_STATE_TTL = 86400.0
"""Seconds a circuit's stored state survives without activity.

Once the lifetime lapses the backend reclaims the entry, and the next
call on that circuit starts from a clean `CLOSED`.
"""

_STATE_TTL_RESET_FACTOR = 10.0
"""Multiple of a circuit's cool-down that floors its stored lifetime.

An `OPEN` circuit is rewritten by nothing: every call is rejected
without touching the store. The floor keeps its lifetime longer than
the cool-down it is waiting out.
"""


def _resolve_state_ttl(reset_timeout: float) -> float:
    """Return the stored-state lifetime for a circuit's `reset_timeout`."""
    return max(_STATE_TTL, _STATE_TTL_RESET_FACTOR * reset_timeout)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from pydantic import Discriminator, PositiveFloat, PositiveInt

    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        CircuitBreakerSnapshot,
        CircuitBreakerStrategy,
    )
    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )
    from grelmicro.types import LogLevel

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

logger = getLogger("grelmicro")


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


@dataclass(frozen=True, slots=True)
class ErrorDetails:
    """Details about an error recorded by the circuit breaker."""

    time: datetime
    type: str
    msg: str


@dataclass(frozen=True, slots=True)
class CircuitBreakerMetrics:
    """Circuit breaker metrics."""

    name: str
    state: CircuitBreakerState
    active_calls: int
    total_error_count: int
    total_success_count: int
    consecutive_error_count: int
    consecutive_success_count: int
    last_error: ErrorDetails | None


def _resolve_algorithm(
    name: str,
    kwargs: dict[str, object | None],
    *,
    env_load: bool | None,
) -> CircuitBreakerConfig:
    """Resolve the consecutive-count fields from kwargs and the environment.

    The algorithm is chosen by the caller. The environment only fills the
    fields that algorithm declares, so a variable naming another
    algorithm's field is reported rather than applied.
    """
    from grelmicro.resilience.circuitbreaker.consecutive_count import (  # noqa: PLC0415
        ConsecutiveCountConfig,
    )

    instance_prefix, kind_prefix = env_prefixes("CIRCUITBREAKER", name)
    return resolve_config(
        ConsecutiveCountConfig,
        explicit=None,
        kwargs=kwargs,
        env_prefix=instance_prefix,
        kind_env_prefix=kind_prefix,
        env_load=env_load,
        union=_union_for_env(),
    )


def _union_for_env() -> object:
    """Return the algorithm union, for cross-arm environment reporting.

    Built from the alias rather than from one arm, so a new algorithm
    joining the union is covered without touching this call.
    """
    from pydantic import Discriminator  # noqa: PLC0415

    from grelmicro.resilience.circuitbreaker.consecutive_count import (  # noqa: PLC0415
        ConsecutiveCountConfig,
    )

    return Annotated[ConsecutiveCountConfig, Discriminator("kind")]


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
        *,
        backend: Annotated[
            CircuitBreakerBackend | str | None,
            Doc(
                """
                The circuit breaker backend that owns the lifespan
                and (with a shared adapter) the cross-replica state.

                Accepts a backend instance, the name of a registered
                backend (e.g. ``"analytics"``), or ``None`` to fall
                back to the registered ``"default"`` Component.
                """
            ),
        ] = None,
        maxsize: Annotated[
            int,
            Doc(
                """
                Per-key circuits kept resident by
                [`keyed`][grelmicro.resilience.CircuitBreaker.keyed].

                `0` keeps every key. Only use it when the key set is
                bounded.
                """
            ),
        ] = _KEYED_MAXSIZE,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read `GREL_CIRCUITBREAKER_*` environment
                variables. When `None` (the default), follow
                `GREL_ENV_LOAD`.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the circuit breaker, defaulting the algorithm to consecutive-count."""
        self._setup(
            name,
            _resolve_algorithm(name, {}, env_load=env_load),
            backend,
            register=True,
            maxsize=maxsize,
        )

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
        maxsize: Annotated[
            int,
            Doc(
                "Per-key circuits kept resident by `keyed`. `0` keeps every key."
            ),
        ] = _KEYED_MAXSIZE,
    ) -> Self:
        """Construct a `CircuitBreaker` from a name and a pre-built `CircuitBreakerConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend, maxsize=maxsize)  # noqa: SLF001
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
        maxsize: Annotated[
            int,
            Doc(
                "Per-key circuits kept resident by `keyed`. `0` keeps every key."
            ),
        ] = _KEYED_MAXSIZE,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read `GREL_CIRCUITBREAKER_*` environment
                variables. When `None` (the default), follow
                `GREL_ENV_LOAD`.
                """
            ),
        ] = None,
    ) -> Self:
        """Construct a `CircuitBreaker` running the consecutive-count algorithm.

        Sibling of [`from_config`][grelmicro.resilience.CircuitBreaker.from_config]
        and the bare constructor. Fields not passed here resolve from
        `GREL_CIRCUITBREAKER_{NAME}_*`, then `GREL_CIRCUITBREAKER_*`, then
        the model default, when env loading is on. `from_config` is the
        static door and reads no variable.
        """
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            name,
            _resolve_algorithm(
                name,
                {
                    "ignore_exceptions": ignore_exceptions,
                    "error_threshold": error_threshold,
                    "success_threshold": success_threshold,
                    "reset_timeout": reset_timeout,
                    "half_open_capacity": half_open_capacity,
                    "log_level": log_level,
                },
                env_load=env_load,
            ),
            backend,
            register=True,
            maxsize=maxsize,
        )
        return instance

    def _setup(
        self,
        name: str,
        config: CircuitBreakerConfig,
        backend: CircuitBreakerBackend | str | None,
        *,
        register: bool = False,
        maxsize: int = _KEYED_MAXSIZE,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance.

        Registers the instance for external reload under
        `GREL_CIRCUITBREAKER_` for the default instance
        (`GREL_CIRCUITBREAKER_{NAME}_` for a named one) when `register`
        is true. The declarative `from_config` path passes
        `register=False` and stays static.
        """
        self._name = name
        self._key: str | None = None
        # Metrics are attributed to the breaker, never to a per-key
        # circuit, so a dynamic key set cannot multiply time series.
        self._metric_name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        # Per-key circuits, most recently used last.
        self._keyed: OrderedDict[str, _KeyedCircuitBreaker] = OrderedDict()
        self._keyed_maxsize = maxsize
        self._touched = 0.0
        if register:
            self._track_reconfigure(default_env_prefix("CIRCUITBREAKER", name))
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

    @overload
    def __call__[**P, R](
        self, func: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R]]: ...

    @overload
    def __call__[**P, R](self, func: Callable[P, R]) -> Callable[P, R]: ...

    @overload
    def __call__(self, func: None = None) -> Self: ...

    def __call__(
        self, func: Callable[..., Any] | None = None
    ) -> Callable[..., Any] | Self:
        """Return a decorator that protects a function with the circuit breaker."""
        if func is None:
            return self

        refuse_registered(func, f"CircuitBreaker {self._name!r}")

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

    def keyed(
        self,
        key: Annotated[
            str,
            Doc(
                """
                Identifier for the circuit (e.g. tenant, endpoint,
                model).

                The breaker's `name` already namespaces the backend
                key, so the circuit is stored under `name:key`.
                """
            ),
        ],
    ) -> CircuitBreaker:
        """Return the circuit for `key`, creating it on first use.

        Each key gets independent counters, state, and cool-down, so
        one failing tenant opens its own circuit while every other key
        keeps calling. The returned breaker supports the same block,
        decorator, `from_thread`, `isolate`, `reset`, `state`, and
        `metrics` surface as the unkeyed breaker.

        Circuits are kept resident up to `maxsize` and the least
        recently used are evicted. A circuit with calls in flight, or
        used within the residency window, is never evicted.

        Evicting an open circuit is safe. The backend owns the state,
        so the next call on that key rebinds and reads the same `OPEN`
        back.
        """
        keyed = self._keyed
        circuit = keyed.get(key)
        if circuit is not None:
            keyed.move_to_end(key)
            circuit._touched = monotonic()  # noqa: SLF001
            return circuit
        circuit = _KeyedCircuitBreaker(self, key)
        keyed[key] = circuit
        self._evict_keyed()
        return circuit

    def _evict_keyed(self) -> None:
        """Drop least recently used circuits while over the budget.

        Skips circuits with calls in flight, and circuits still inside
        the residency window. Stops when nothing is evictable, letting
        the map exceed `maxsize` rather than dropping a busy circuit.

        Evicting an open circuit is safe. The backend owns the state,
        so the next call on that key rebinds and reads the same
        `OPEN` back. Only the per-replica counters and `last_error`
        reset, which are already documented as per-replica.
        """
        keyed = self._keyed
        maxsize = self._keyed_maxsize
        if not maxsize:
            return
        now = monotonic()
        while len(keyed) > maxsize:
            for key, circuit in keyed.items():
                if circuit._evictable(now):  # noqa: SLF001
                    del keyed[key]
                    break
            else:
                return

    def _evictable(self, now: float) -> bool:
        """Whether this circuit can be dropped from the parent's map."""
        return (
            self._active_call_count == 0
            and now - self._touched >= _KEYED_RESIDENCY
        )

    @property
    def backend(self) -> CircuitBreakerBackend:
        """Bound circuit breaker backend, resolved on each call.

        Resolution order:
        1. An explicit `backend=` passed at construction wins.
        2. The active `Grelmicro` app is consulted on every access
           so that `micro.override(...)` blocks
           take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope. Pass
                `backend=` (a `MemoryCircuitBreakerAdapter()` for a
                per-replica breaker), register a `CircuitBreakerComponent`
                Component, or run the call inside `async with micro:` or
                after `micro.install(app)`.
        """
        if self._backend is not None:
            return self._backend
        try:
            component = resolve_ambient(
                ("circuitbreaker", self._backend_name or "default")
            )
        except LookupError:
            msg = (
                f"CircuitBreaker({self._name!r}) resolved no backend. Pass "
                f"backend= (MemoryCircuitBreakerAdapter() for a per-replica "
                f"breaker), register a CircuitBreakerComponent component, or run "
                f"the call inside `async with micro:` or after "
                f"`micro.install(app)`."
            )
            raise OutOfContextError(msg) from None
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
        loop: asyncio.AbstractEventLoop | None = backend._loop  # noqa: SLF001
        if loop is None:  # pragma: no cover
            backend._loop = asyncio.get_running_loop()  # noqa: SLF001
        state = self._state
        strategy = state.strategy or self._resolve_strategy(state)
        if not await strategy.try_acquire():
            _emit.incr(
                "grelmicro.circuit_breaker.calls",
                **{
                    "circuit_breaker.name": self._metric_name,
                    "result": "rejected",
                },
            )
            snapshot = await strategy.get_snapshot()
            self._apply_snapshot(snapshot)
            raise CircuitBreakerError(
                name=self.name,
                last_error_time=self._last_error_time,
                last_error=self._last_error,
                retry_after=snapshot.retry_after,
            )
        self._active_call_count += 1
        self._touched = monotonic()
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
            result = "success"
        elif isinstance(exc_value, Exception):
            snapshot = await strategy.record_outcome(success=False)
            self._total_error_count += 1
            self._last_error = exc_value
            self._last_error_time = datetime.now(UTC)
            result = "error"
        else:
            await strategy.abandon()
            return None
        _emit.incr(
            "grelmicro.circuit_breaker.calls",
            **{"circuit_breaker.name": self._metric_name, "result": result},
        )
        self._apply_snapshot(snapshot)
        return None

    def _apply_snapshot(self, snapshot: CircuitBreakerSnapshot) -> None:
        """Refresh local cache from the strategy snapshot and log transitions."""
        previous = self._cached_state
        new = snapshot.state
        self._cached_state = new
        self._consecutive_error_count = snapshot.consecutive_error_count
        self._consecutive_success_count = snapshot.consecutive_success_count
        _emit.observe(
            "grelmicro.circuit_breaker.state",
            _STATE_CODE.get(new, -1),
            **{"circuit_breaker.name": self._metric_name},
        )
        if previous != new:
            _emit.incr(
                "grelmicro.circuit_breaker.transitions",
                **{
                    "circuit_breaker.name": self._metric_name,
                    "from": str(previous),
                    "to": str(new),
                },
            )
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
    def key(self) -> str | None:
        """Return the key this circuit is bound to, or `None` when unkeyed.

        Set on the circuits returned by
        [`keyed`][grelmicro.resilience.CircuitBreaker.keyed].
        """
        return self._key

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

    async def isolate(self) -> None:
        """Force the breaker open and keep it open until reset.

        The manual "big red button". Moves the breaker to
        `FORCED_OPEN`, so every call is blocked with
        `CircuitBreakerError` regardless of outcomes, until
        [`reset`][grelmicro.resilience.CircuitBreaker.reset] returns it
        to automatic operation.
        """
        await self._transition(
            CircuitBreakerState.FORCED_OPEN, _TransitionCause.MANUAL
        )

    async def reset(self) -> None:
        """Return the breaker to normal automatic operation.

        Clears all counters and the last recorded error, then moves the
        breaker to `CLOSED`. Use it to release an
        [`isolate`][grelmicro.resilience.CircuitBreaker.isolate] hold or
        to start fresh from a known state.
        """
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error = None
        self._last_error_time = None
        await self._transition(
            CircuitBreakerState.CLOSED, _TransitionCause.RESTART
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
        if self._active_call_count > 0:  # pragma: no branch
            self._active_call_count -= 1
        # Residency runs from the end of the call, so a call that
        # outlives the window does not leave its circuit evictable the
        # moment it returns.
        self._touched = monotonic()

    async def _apply_reconfigure(
        self, new_config: CircuitBreakerConfig
    ) -> None:
        """Rebind the strategy with the new config and update the logger level."""
        self._logger.setLevel(new_config.log_level)
        # Clear the cached strategy. The next call rebinds it through
        # `_resolve_strategy` with the freshly published config.
        self._state = _State(config=new_config, strategy=None)
        # Per-key circuits track the breaker's config, so clear theirs
        # too and let the next call on each key rebind.
        for circuit in self._keyed.values():
            circuit._config = new_config  # noqa: SLF001
            circuit._state = _State(config=new_config, strategy=None)  # noqa: SLF001

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


class _KeyedCircuitBreaker(CircuitBreaker):
    """One per-key circuit of a keyed `CircuitBreaker`.

    Shares the breaker's logger, admission-config stack, reconfigure
    lock, and backend binding. Owns the storage identity `name:key`,
    its own bound strategy, and its own counters, so each key trips
    independently.

    The logger and `ContextVar` stay shared because both are
    process-global once created: `getLogger` retains every name for the
    life of the process, and a `ContextVar` is held strongly by any
    `Context` that saw it.
    """

    def __init__(self, breaker: CircuitBreaker, key: str) -> None:
        """Bind a circuit for `key` onto the breaker's shared machinery."""
        self._key = key
        self._name = f"{breaker._name}:{key}"  # noqa: SLF001
        # Metrics stay attributed to the breaker, so a dynamic key set
        # never multiplies time series. Logs carry `_name` instead and
        # keep the full identity.
        self._metric_name = breaker._metric_name  # noqa: SLF001
        self._config = breaker._config  # noqa: SLF001
        self._reconfigure_lock = breaker._reconfigure_lock  # noqa: SLF001
        self._backend = breaker._backend  # noqa: SLF001
        self._backend_name = breaker._backend_name  # noqa: SLF001
        self._enter_stack = breaker._enter_stack  # noqa: SLF001
        self._logger = breaker._logger  # noqa: SLF001
        self._from_thread: _ThreadAdapter | None = None
        self._state = _State(config=breaker._state.config, strategy=None)  # noqa: SLF001
        self._cached_state = CircuitBreakerState.CLOSED
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error: Exception | None = None
        self._last_error_time: datetime | None = None
        self._active_call_count = 0
        self._touched = monotonic()
        # A per-key circuit is a leaf: it is not itself keyed, and it
        # is not tracked for external reload (the breaker is).
        self._keyed: OrderedDict[str, _KeyedCircuitBreaker] = OrderedDict()
        self._keyed_maxsize = 0

    def keyed(self, key: str) -> CircuitBreaker:  # noqa: ARG002
        """Raise, because a per-key circuit cannot be keyed again."""
        msg = (
            f"CircuitBreaker({self._name!r}) is already keyed on "
            f"{self._key!r}. Call keyed() on the breaker instead."
        )
        raise ValueError(msg)

    async def reconfigure(
        self,
        new_config: CircuitBreakerConfig,  # noqa: ARG002
    ) -> None:
        """Raise, because config is published by the breaker."""
        msg = (
            f"CircuitBreaker({self._name!r}) is a per-key circuit. "
            f"Call reconfigure() on the breaker instead."
        )
        raise ValueError(msg)


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
        loop = backend._loop  # noqa: SLF001
        if loop is None:
            raise_backend_not_open(f"CircuitBreaker {cb.name!r}")
        if on_backend_loop(loop):
            raise_event_loop_deadlock(
                f"CircuitBreaker {cb.name!r} `from_thread`",
                "Use `async with breaker:` from async code, or run the sync "
                "call through `asyncio.to_thread(...)`.",
            )
        config = cb._state.config  # noqa: SLF001
        snapshot = asyncio.run_coroutine_threadsafe(
            _async_admit(cb), loop
        ).result()
        if snapshot is not None:
            raise CircuitBreakerError(
                name=cb.name,
                last_error_time=cb.last_error_time,
                last_error=cb.last_error,
                retry_after=snapshot.retry_after,
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
        loop = cb.backend._loop  # noqa: SLF001
        if loop is None:  # pragma: no cover
            # `__enter__` already resolved the loop, so this only guards
            # against a backend closed mid-context.
            raise_backend_not_open(f"CircuitBreaker {cb.name!r}")
        asyncio.run_coroutine_threadsafe(
            _async_handle_exit(cb, config, exc_type, exc_value), loop
        ).result()
        return None


async def _async_admit(cb: CircuitBreaker) -> CircuitBreakerSnapshot | None:
    """Try to acquire a call. Runs on the backend loop.

    Returns `None` when the call is admitted, and the refusing snapshot
    otherwise, so the worker thread can report how long the circuit still
    has to wait out.

    Mutates per-replica counters from the loop thread so worker threads
    never touch breaker state directly.
    """
    state = cb._state  # noqa: SLF001
    strategy = state.strategy or cb._resolve_strategy(state)  # noqa: SLF001
    if await strategy.try_acquire():
        cb._active_call_count += 1  # noqa: SLF001
        cb._touched = monotonic()  # noqa: SLF001
        return None
    snapshot = await strategy.get_snapshot()
    cb._apply_snapshot(snapshot)  # noqa: SLF001
    return snapshot


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
    else:
        await strategy.abandon()
        return
    cb._apply_snapshot(snapshot)  # noqa: SLF001
