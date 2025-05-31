"""Circuit Breaker."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from logging import Logger, getLogger
from time import monotonic
from typing import ClassVar

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
    """Circuit is forced open by manual intervention, calls are not allowed."""
    FORCED_CLOSED = "FORCED_CLOSED"
    """Circuit is forced closed by manual intervention, calls are allowed."""


@dataclass(frozen=True)
class ErrorInfo:
    """Information about an error recorded by the circuit breaker.

    Attributes:
        time: When the error occurred.
        error: The exception that was raised.
    """

    time: datetime
    error: Exception


@dataclass(frozen=True)
class CircuitBreakerStatistics:
    """Statistics for a circuit breaker.

    Attributes:
        name: Name of the circuit breaker.
        state: Current state of the circuit breaker.
        last_state_change_time: Timestamp of when the circuit breaker last changed its state.
        active_calls: Number of calls currently active through the circuit breaker.
        total_error_count: Lifetime total number of errors recorded.
        total_success_count: Lifetime total number of successes recorded.
        consecutive_error_count: Number of consecutive errors since last success or reset of consecutive counts.
        consecutive_success_count: Number of successful calls since last error or reset of consecutive counts.
        last_error_info: Information about the last error recorded, if any.
        last_consecutive_counts_cleared_at: Timestamp of when the consecutive counts were last cleared.
        creation_time: Timestamp of when the circuit breaker instance was created.
    """

    name: str
    state: CircuitBreakerState
    last_state_change_time: datetime
    active_calls: int
    total_error_count: int
    total_success_count: int
    consecutive_error_count: int
    consecutive_success_count: int
    last_error_info: ErrorInfo | None
    last_consecutive_counts_cleared_at: datetime | None
    creation_time: datetime


class CircuitBreakerError(Exception):
    """Circuit breaker error.

    Raised when calls are not permitted by the circuit breaker.
    """

    def __init__(self, *, last_error_info: ErrorInfo | None) -> None:
        """Initialize the error."""
        self.last_error_info = last_error_info
        super().__init__(self._generate_message())

    def _generate_message(self) -> str:
        """Generate a string representation of the error."""
        if self.last_error_info:
            last_error_at_str = self.last_error_info.time.isoformat()
            last_error_type_str = type(self.last_error_info.error).__name__
            return (
                f"Circuit breaker error: calls not permitted. "
                f"Last error: {last_error_type_str} at {last_error_at_str}"
            )
        return "Circuit breaker error: calls not permitted."

    def __str__(self) -> str:
        """Return a string representation of the error."""
        return self._generate_message()


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
        name: str,
        *,
        error_threshold: int = 5,
        success_threshold: int = 2,
        reset_timeout: float = 60.0,
        half_open_max_concurrency: int = 1,
        consecutive_counts_clear_interval: float = 0.0,
        logger: Logger | None = None,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            name: Name of the circuit breaker instance.
            error_threshold: Number of consecutive errors before opening the circuit.
            success_threshold: Number of consecutive successes in HALF_OPEN state
                before closing the circuit.
            reset_timeout: Duration (in seconds) the circuit stays in the OPEN state
                before transitioning to HALF_OPEN.
            half_open_max_concurrency: Maximum number of concurrent calls allowed
                in the HALF_OPEN state.
            consecutive_counts_clear_interval: Cyclic period (in seconds) in the CLOSED state
                to clear consecutive error and success counts. If 0 or negative,
                consecutive counts are not cleared periodically.
            logger: Logger for logging events. Defaults to `grelmicro.circuitbreaker.{name}`.
        """
        self.name = name
        self.error_threshold = error_threshold
        self.success_threshold = success_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max_concurrency = half_open_max_concurrency
        self.consecutive_counts_clear_interval = (
            consecutive_counts_clear_interval
        )

        self.logger = logger or getLogger(
            f"grelmicro.circuitbreaker.{self.name}"
        )

        self._state = CircuitBreakerState.CLOSED
        self._creation_time = datetime.now(UTC)
        self._last_state_change_time = self._creation_time

        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._total_error_count = 0
        self._total_success_count = 0

        self._last_error_info: ErrorInfo | None = None
        self._open_until_time = 0.0  # Monotonic time
        self._active_calls = 0

        self._last_consecutive_counts_cleared_at: datetime | None = None
        self._next_consecutive_counts_clear_time = (
            (monotonic() + self.consecutive_counts_clear_interval)
            if self.consecutive_counts_clear_interval > 0
            else 0.0
        )
        if (
            self.consecutive_counts_clear_interval > 0
        ):  # Initialize if interval is set
            self._last_consecutive_counts_cleared_at = datetime.now(UTC)

        self.logger.info(
            "Circuit breaker '%s' initialized. State: %s, ErrorThreshold: %d, SuccessThreshold: %d, ResetTimeout: %.2fs",
            self.name,
            self._state,
            self.error_threshold,
            self.success_threshold,
            self.reset_timeout,
        )

    @property
    def current_state(self) -> CircuitBreakerState:
        """Return the current state of the circuit breaker, refreshing if necessary."""
        self._refresh_state()
        return self._state

    @property
    def last_error(self) -> ErrorInfo | None:
        """Return information about the last recorded error, if any."""
        return self._last_error_info

    def statistics(self) -> CircuitBreakerStatistics:
        """Return current statistics for this circuit breaker."""
        # Ensure state is up-to-date before getting stats
        current_s = self.current_state
        return CircuitBreakerStatistics(
            name=self.name,
            state=current_s,
            last_state_change_time=self._last_state_change_time,
            active_calls=self._active_calls,
            total_error_count=self._total_error_count,
            total_success_count=self._total_success_count,
            consecutive_error_count=self._consecutive_error_count,
            consecutive_success_count=self._consecutive_success_count,
            last_error_info=self._last_error_info,
            last_consecutive_counts_cleared_at=self._last_consecutive_counts_cleared_at,
            creation_time=self._creation_time,
        )

    def _do_transition_to_state(self, new_state: CircuitBreakerState) -> None:
        """Internal helper to change state and update timestamp."""
        if self._state != new_state:
            self.logger.info(
                "Circuit breaker '%s' transitioning from %s to %s.",
                self.name,
                self._state,
                new_state,
            )
            self._state = new_state
            self._last_state_change_time = datetime.now(UTC)
        else:
            self.logger.debug(
                "Circuit breaker '%s' already in state %s. No transition.",
                self.name,
                new_state,
            )

    def _transition_to_state(self, new_state: CircuitBreakerState) -> None:
        """Transition the circuit breaker to a new state and perform associated actions."""
        previous_state = self._state
        self._do_transition_to_state(new_state)

        if new_state == CircuitBreakerState.OPEN:
            self._open_until_time = monotonic() + self.reset_timeout
            self.logger.error(
                "Circuit breaker '%s' opened. Will attempt to transition to HALF_OPEN after %.2f seconds.",
                self.name,
                self.reset_timeout,
            )
        elif new_state == CircuitBreakerState.HALF_OPEN:
            self.clear_consecutive_counts()  # Reset for probing
            self.logger.info(
                "Circuit breaker '%s' is now HALF_OPEN. Allowing up to %d concurrent call(s) for probing.",
                self.name,
                self.half_open_max_concurrency,
            )
        elif (
            new_state == CircuitBreakerState.CLOSED
            and previous_state == CircuitBreakerState.HALF_OPEN
        ):
            self.clear_consecutive_counts()  # Reset after successful probing
            self.logger.info(
                "Circuit breaker '%s' closed after successful probing in HALF_OPEN state.",
                self.name,
            )
        elif (
            new_state == CircuitBreakerState.CLOSED
            and previous_state != CircuitBreakerState.HALF_OPEN
        ):
            self.logger.info("Circuit breaker '%s' is now CLOSED.", self.name)

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
        self._refresh_state()
        if not self._is_call_permitted():
            self.logger.warning(
                "Circuit breaker '%s' denied call in %s state.",
                self.name,
                self._state,
            )
            raise CircuitBreakerError(last_error_info=self._last_error_info)

        self._active_calls += 1
        try:
            yield
        except ignore_errors:
            # This error is ignored for circuit breaking purposes, but still re-raised.
            # It does not count as a success or failure for the circuit breaker.
            self.logger.debug(
                "Circuit breaker '%s' ignored error due to ignore_errors configuration.",
                self.name,
            )
            raise
        except Exception as error:
            self._on_error(error)
            raise
        else:
            self._on_success()
        finally:
            self._active_calls -= 1
            self._after_call_check()

    def _is_call_permitted(self) -> bool:
        """Check if a call is permitted based on the current state."""
        if self._state == CircuitBreakerState.CLOSED:
            return True
        if self._state == CircuitBreakerState.HALF_OPEN:
            return self._active_calls < self.half_open_max_concurrency
        # OPEN, FORCED_OPEN, FORCED_CLOSED (handled by _refresh_state for forced states)
        return False

    def _on_error(self, error: Exception) -> None:
        """Record an error, update counts, and potentially transition state."""
        self._total_error_count += 1
        self._consecutive_success_count = 0
        self._consecutive_error_count += 1
        self._last_error_info = ErrorInfo(time=datetime.now(UTC), error=error)
        self.logger.debug(
            "Circuit breaker '%s' recorded an error: %s. Consecutive errors: %d.",
            self.name,
            type(error).__name__,
            self._consecutive_error_count,
        )

        if self._state == CircuitBreakerState.HALF_OPEN:
            self.logger.warning(
                "Circuit breaker '%s' failed in HALF_OPEN state. Re-opening.",
                self.name,
            )
            self._transition_to_state(CircuitBreakerState.OPEN)
        # Transition from CLOSED to OPEN is handled in _after_call_check

    def _on_success(self) -> None:
        """Record a success, update counts, and potentially transition state."""
        self._total_success_count += 1
        self._consecutive_error_count = 0
        self._consecutive_success_count += 1
        self.logger.debug(
            "Circuit breaker '%s' recorded a success. Consecutive successes: %d.",
            self.name,
            self._consecutive_success_count,
        )
        # Transition from HALF_OPEN to CLOSED is handled in _after_call_check

    def _refresh_state(self) -> None:
        """Refresh the circuit breaker's state based on time or conditions."""
        if self._state == CircuitBreakerState.CLOSED:
            if (
                self.consecutive_counts_clear_interval > 0
                and self._next_consecutive_counts_clear_time <= monotonic()
            ):
                self.logger.debug(
                    "Circuit breaker '%s' clearing consecutive counts due to interval.",
                    self.name,
                )
                self.clear_consecutive_counts()
        elif self._state == CircuitBreakerState.OPEN:
            if monotonic() >= self._open_until_time:
                self.logger.info(
                    "Circuit breaker '%s' reset timeout expired. Transitioning to HALF_OPEN.",
                    self.name,
                )
                self._transition_to_state(CircuitBreakerState.HALF_OPEN)
        # FORCED_OPEN and FORCED_CLOSED states are not changed by _refresh_state

    def _after_call_check(self) -> None:
        """Check and transition state after a call completes (successfully or with error)."""
        if self._state == CircuitBreakerState.CLOSED:
            if self._consecutive_error_count >= self.error_threshold:
                self.logger.warning(
                    "Circuit breaker '%s' reached error threshold (%d). Opening circuit.",
                    self.name,
                    self.error_threshold,
                )
                self._transition_to_state(CircuitBreakerState.OPEN)
        elif self._state == CircuitBreakerState.HALF_OPEN:
            # Error in HALF_OPEN is handled by _on_error to immediately re-open.
            # Here, we only check for success.
            if self._consecutive_success_count >= self.success_threshold:
                self.logger.info(
                    "Circuit breaker '%s' reached success threshold (%d) in HALF_OPEN. Closing circuit.",
                    self.name,
                    self.success_threshold,
                )
                self._transition_to_state(CircuitBreakerState.CLOSED)

    def clear_consecutive_counts(self) -> None:
        """Reset only the consecutive error and success counts."""
        self.logger.debug(
            "Circuit breaker '%s' resetting consecutive counts.", self.name
        )
        self._consecutive_error_count = 0
        self._consecutive_success_count = 0
        self._last_consecutive_counts_cleared_at = datetime.now(UTC)
        if self.consecutive_counts_clear_interval > 0:
            self._next_consecutive_counts_clear_time = (
                monotonic() + self.consecutive_counts_clear_interval
            )

    def reset(self) -> None:
        """Reset the circuit breaker to its initial CLOSED state and clear all counts (total and consecutive)."""
        self.logger.warning("Circuit breaker '%s' is being reset.", self.name)
        self._do_transition_to_state(
            CircuitBreakerState.CLOSED
        )  # Sets state and last_state_change_time
        self._total_error_count = 0
        self._total_success_count = 0
        self._last_error_info = None
        self._open_until_time = 0.0
        # _active_calls is not reset as it reflects current ongoing operations,
        # though a full reset might imply no active calls should persist.
        # Consider if _active_calls should be 0. For now, it's not touched.
        self.clear_consecutive_counts()  # Resets consecutive and updates relevant timestamps

    def force_open(self, open_duration: float | None = None) -> None:
        """Force the circuit breaker to the OPEN state.

        Args:
            open_duration: Optional duration in seconds for the circuit to remain OPEN.
                If None, uses the configured `reset_timeout`.
        """
        self.logger.warning(
            "Circuit breaker '%s' is being forced OPEN.", self.name
        )
        self._do_transition_to_state(CircuitBreakerState.FORCED_OPEN)
        self._open_until_time = monotonic() + (
            open_duration if open_duration is not None else self.reset_timeout
        )
        self.clear_consecutive_counts()

    def force_closed(self) -> None:
        """Force the circuit breaker to the CLOSED state."""
        self.logger.warning(
            "Circuit breaker '%s' is being forced CLOSED.", self.name
        )
        self._do_transition_to_state(CircuitBreakerState.FORCED_CLOSED)
        self.clear_consecutive_counts()

    def force_half_open(self) -> None:
        """Force the circuit breaker to the HALF_OPEN state (primarily for testing)."""
        self.logger.warning(
            "Circuit breaker '%s' is being forced HALF_OPEN.", self.name
        )
        self._do_transition_to_state(CircuitBreakerState.HALF_OPEN)
        self.clear_consecutive_counts()  # HALF_OPEN starts with fresh consecutive counts

    # The set_state method was quite similar to force methods.
    # If specific behavior is needed beyond force_open/closed/half_open, it can be re-added.
    # For now, using the more specific force methods is preferred.
    # def set_state(self, state: CircuitBreakerState) -> None: ...
