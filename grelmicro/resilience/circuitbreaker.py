"""Circuit Breaker."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from logging import Logger, getLogger
from time import monotonic
from typing import Annotated, ClassVar

from typing_extensions import Doc

_ErrorTypes = type[Exception] | tuple[type[Exception], ...]


class CircuitBreakerState(StrEnum):
    """Circuit breaker state.

    State machine diagram:
    ```
    ┌────────┐ errors >= threshold  ┌─────────┐
    │ CLOSED │────────────────────> │  OPEN   │ <─┐
    └────────┘                      └─────────┘   │
        ▲                               │         │
        │                       timeout │         │ errors >= threshold
        │                               ▼         │
        │                         ┌───────────┐   │
        └─────────────────────────│ HALF_OPEN │───┘
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


class TransitionCause(StrEnum):
    """Cause of a circuit breaker state transition."""

    ERROR_THRESHOLD = "ERROR_THRESHOLD"
    """Transition due to reaching the error threshold."""
    SUCCESS_THRESHOLD = "SUCCESS_THRESHOLD"
    """Transition due to reaching the success threshold."""
    RESET_TIMEOUT = "RESET_TIMEOUT"
    """Transition due to timeout after the circuit was open."""
    MANUAL = "MANUAL"
    """Transition due to manual intervention."""
    RESTART = "RESTART"
    """Transition due to circuit breaker restart."""


@dataclass(frozen=True)
class ErrorDetails:
    """Details about an error recorded by the circuit breaker.

    Attributes:
        time: When the error occurred.
        type: Type of the error.
        msg: Error message.
    """

    time: datetime
    type: str
    msg: str


@dataclass(frozen=True)
class CircuitBreakerMetrics:
    """Circuit breaker metrics."""

    name: str
    state: CircuitBreakerState
    active_call_count: int
    total_error_count: int
    total_success_count: int
    consecutive_error_count: int
    consecutive_success_count: int
    last_error: ErrorDetails | None


class CircuitBreakerError(Exception):
    """Circuit breaker error.

    Raised when calls are not permitted by the circuit breaker.
    """

    def __init__(self, name: str, last_error: ErrorDetails | None) -> None:
        """Initialize the error."""
        self.name = name
        self.last_error = last_error
        super().__init__(f"Circuit breaker '{name}': call not permitted")


class CircuitBreakerRegistry:
    """Registry for circuit breakers.

    This registry is used to store and retrieve circuit breaker instances by name.
    Circuit breakers are automatically registered when created.
    """

    _instances: ClassVar[dict[str, "CircuitBreaker"]] = {}

    @classmethod
    def get(cls, name: str) -> "CircuitBreaker | None":
        """Get a circuit breaker by name.

        Args:
            name: Name of the circuit breaker.

        Returns:
            The circuit breaker instance or None if not found.
        """
        return cls._instances.get(name)

    @classmethod
    def register(cls, circuit_breaker: "CircuitBreaker") -> None:
        """Register a circuit breaker.

        Args:
            circuit_breaker: The circuit breaker instance to register.
        """
        if circuit_breaker.name in cls._instances:
            # TODO
            # This case should ideally be handled by the metaclass logic
            # or raise an error if re-registration with the same name is not allowed.
            # For now, it overwrites, which might be intended by the metaclass.
            pass
        cls._instances[circuit_breaker.name] = circuit_breaker

    @classmethod
    def get_all(cls) -> list["CircuitBreaker"]:
        """Get all registered circuit breakers.

        Returns:
            List of all registered circuit breaker instances.
        """
        return list(cls._instances.values())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered circuit breakers (primarily for testing)."""
        cls._instances.clear()


class _CircuitBreakerMeta(type):
    """Metaclass for CircuitBreaker to handle instance registry."""

    def __call__(cls, name: str, **kwargs: object) -> "CircuitBreaker":
        """Get existing circuit breaker or create and register a new one."""
        existing = CircuitBreakerRegistry.get(name)
        if existing is not None:
            # TODO: Consider if re-configuration of an existing instance should be allowed
            # or if an error should be raised if kwargs are different.
            # For now, it returns the existing one without re-configuring.
            return existing

        instance = super().__call__(name, **kwargs)
        CircuitBreakerRegistry.register(instance)
        return instance


class CircuitBreaker(metaclass=_CircuitBreakerMeta):
    """Circuit Breaker.

    Implements the circuit breaker pattern to prevent cascading failures
    by monitoring and controlling calls to a protected service.

    Usage:
        cb = CircuitBreaker("my-service-cb")
        try:
            async with cb.guard():
                # Call protected service
                result = await protected_service_call()
        except CircuitBreakerError:
            # Handle circuit open, e.g., return a fallback
        except OtherExpectedError:
            # Handle specific errors from the protected service
    """

    def __init__(
        self,
        name: Annotated[str, Doc("Name of the circuit breaker instance.")],
        *,
        error_threshold: Annotated[
            int, Doc("Number of consecutive errors before opening the circuit.")
        ] = 5,
        success_threshold: Annotated[
            int,
            Doc(
                "Number of consecutive successes in HALF_OPEN state before closing the circuit."
            ),
        ] = 2,
        reset_timeout: Annotated[
            float,
            Doc(
                "Duration (in seconds) the circuit stays in the OPEN state before transitioning to HALF_OPEN."
            ),
        ] = 30,
        half_open_capacity: Annotated[
            int,
            Doc(
                "Maximum number of concurrent calls allowed in the HALF_OPEN state."
            ),
        ] = 1,
        logger: Logger | None = None,
    ) -> None:
        """Initialize the circuit breaker."""
        self.error_threshold = error_threshold
        self.success_threshold = success_threshold
        self.reset_timeout = reset_timeout
        self.half_open_capacity = half_open_capacity
        self.logger = logger or getLogger(f"grelmicro.circuitbreaker.{name}")

        self._name = name
        self._state = CircuitBreakerState.CLOSED
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error: ErrorDetails | None = None
        self._open_until_time = 0.0  # Monotonic time
        self._active_call_count = 0

    @property
    def name(self) -> str:
        """Return the name of the circuit breaker."""
        return self._name

    @property
    def state(self) -> CircuitBreakerState:
        """Return the current state of the circuit breaker, refreshing if necessary."""
        self._refresh_state()
        return self._state

    @property
    def last_error(self) -> ErrorDetails | None:
        """Return information about the last recorded error, if any."""
        return self._last_error

    def metrics(self) -> CircuitBreakerMetrics:
        """Return current metrics for this circuit breaker."""
        return CircuitBreakerMetrics(
            name=self._name,
            state=self.state,  # refreshed state
            active_call_count=self._active_call_count,
            total_error_count=self._total_error_count,
            total_success_count=self._total_success_count,
            consecutive_error_count=self._consecutive_error_count,
            consecutive_success_count=self._consecutive_success_count,
            last_error=self._last_error,
        )

    @asynccontextmanager
    async def guard(
        self, *, ignore_errors: _ErrorTypes = ()
    ) -> AsyncGenerator[None, None]:
        """Guard a block of code with circuit breaker protection.

        Args:
            ignore_errors: A single exception type or a tuple of exception types
                that should not be counted as errors by the circuit breaker.

        Yields:
            None.

        Raises:
            CircuitBreakerError: If the call is not permitted due to the circuit's state.
            Any exception raised by the guarded code block (unless in `ignore_errors`).
        """
        if not self._is_call_permitted():
            raise CircuitBreakerError(self.name, self._last_error)

        self._active_call_count += 1
        try:
            yield
        except ignore_errors:
            self._on_success()
            raise
        except Exception as error:
            self._on_error(error)
            raise
        else:
            self._on_success()
        finally:
            self._active_call_count -= 1

    def _is_call_permitted(self) -> bool:
        """Check if a call is permitted based on the current state."""
        self._refresh_state()
        if self._state in (
            CircuitBreakerState.CLOSED,
            CircuitBreakerState.FORCED_CLOSED,
        ):
            return True
        if self._state == CircuitBreakerState.HALF_OPEN:
            return self._active_call_count < self.half_open_capacity
        return False

    def _on_error(self, error: Exception) -> None:
        """Record an error, update counts, and potentially transition state."""
        self._total_error_count += 1
        self._consecutive_error_count += 1
        self._consecutive_success_count = 0

        self._last_error = ErrorDetails(
            time=datetime.now(UTC), type=type(error).__name__, msg=str(error)
        )

        if (
            self._state != CircuitBreakerState.OPEN
            and self._consecutive_error_count >= self.error_threshold
        ):
            self._do_transition_to_state(
                CircuitBreakerState.OPEN, TransitionCause.ERROR_THRESHOLD
            )

    def _on_success(self) -> None:
        """Record a success, update counts, and potentially transition state."""
        self._total_success_count += 1
        self._consecutive_error_count = 0
        self._consecutive_success_count += 1

        if (
            self._state == CircuitBreakerState.HALF_OPEN
            and self._consecutive_success_count >= self.success_threshold
        ):
            self._do_transition_to_state(
                CircuitBreakerState.CLOSED, TransitionCause.SUCCESS_THRESHOLD
            )

    def _refresh_state(self) -> None:
        """Refresh the circuit breaker's state based on time or conditions."""
        if (
            self._state == CircuitBreakerState.OPEN
            and monotonic() >= self._open_until_time
        ):
            self._do_transition_to_state(
                CircuitBreakerState.HALF_OPEN, TransitionCause.RESET_TIMEOUT
            )

    def restart(self) -> None:
        """Restart the circuit breaker, clearing all counts and resetting to CLOSED state."""
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error = None
        self._do_transition_to_state(
            CircuitBreakerState.CLOSED, TransitionCause.RESTART
        )

    def transition_to_closed(self) -> None:
        """Transition the circuit breaker to CLOSED state."""
        self._do_transition_to_state(
            CircuitBreakerState.CLOSED, TransitionCause.MANUAL
        )

    def transition_to_open(self, until: float | None = None) -> None:
        """Transition the circuit breaker to OPEN state."""
        self._do_transition_to_state(
            CircuitBreakerState.OPEN, TransitionCause.MANUAL, open_until=until
        )

    def transition_to_half_open(self) -> None:
        """Transition the circuit breaker to HALF_OPEN state."""
        self._do_transition_to_state(
            CircuitBreakerState.HALF_OPEN, TransitionCause.MANUAL
        )

    def transition_to_forced_open(self) -> None:
        """Transition the circuit breaker to FORCED_OPEN state."""
        self._do_transition_to_state(
            CircuitBreakerState.FORCED_OPEN, TransitionCause.MANUAL
        )

    def transition_to_forced_closed(self) -> None:
        """Transition the circuit breaker to FORCED_CLOSED state."""
        self._do_transition_to_state(
            CircuitBreakerState.FORCED_CLOSED, TransitionCause.MANUAL
        )

    def _do_transition_to_state(
        self,
        state: CircuitBreakerState,
        cause: TransitionCause,
        open_until: float | None = None,
    ) -> None:
        """Transition to a new state and reset consecutive counts."""
        self._state = state
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0

        self._open_until_time = (
            monotonic()
            + (self.reset_timeout if open_until is None else open_until)
            if state == CircuitBreakerState.OPEN
            else 0
        )

        self.logger.log(
            logging.ERROR
            if state == CircuitBreakerState.OPEN
            else logging.INFO,
            "Circuit breaker '%s': state changed to %s [cause: %s]",
            self.name,
            state,
            cause,
        )
