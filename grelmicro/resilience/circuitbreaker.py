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


@dataclass(frozen=True)
class ErrorInfo:
    """Information about an error that caused the circuit to open.

    Attributes:
        timestamp: When the error occurred
        error: The exception that was raised
    """

    timestamp: datetime
    error: Exception


@dataclass(frozen=True)
class CircuitBreakerStatistics:
    """Statistics for a circuit breaker.

    Attributes:
        name: Name of the circuit breaker
        state: Current state of the circuit breaker
        error_count: Number of consecutive errors since last success
        success_count: Number of successful calls since last error
        last_error: Last error that caused the circuit to open, if any
    """

    name: str
    state: CircuitBreakerState
    active_calls: int
    error_count: int
    success_count: int
    last_error: ErrorInfo | None = None


class CircuitBreakerError(Exception):
    """Circuit breaker error.

    Raised when the calls are not permitted by the circuit breaker.
    """

    def __init__(self, *, last_error: ErrorInfo | None = None) -> None:
        """Initialize the error."""
        self.last_error = last_error

    def __str__(self) -> str:
        """Return a string representation of the error."""
        last_error_time = (
            self.last_error.timestamp.isoformat() if self.last_error else "N/A"
        )
        last_error_type = (
            type(self.last_error.error).__name__ if self.last_error else "N/A"
        )
        return (
            f"Circuit breaker error: calls not permitted"
            f" [last_error_type={last_error_type}, last_error_time={last_error_time}]"
        )


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
            name: Name of the circuit breaker

        Returns:
            The circuit breaker instance or None if not found
        """
        return cls._instances.get(name)

    @classmethod
    def register(cls, circuit_breaker: "CircuitBreaker") -> None:
        """Register a circuit breaker.

        Args:
            circuit_breaker: The circuit breaker instance to register
        """
        cls._instances[circuit_breaker.name] = circuit_breaker

    @classmethod
    def get_all(cls) -> list["CircuitBreaker"]:
        """Get all registered circuit breakers.

        Returns:
            List of all registered circuit breaker instances
        """
        return list(cls._instances.values())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered circuit breakers (primarily for testing)."""
        cls._instances.clear()


class _CircuitBreakerMeta(type):
    """Metaclass for CircuitBreaker to handle instance registry."""

    def __call__(cls, name: str, **kwargs: object) -> "CircuitBreaker":
        """Get existing circuit breaker or create a new one."""
        existing = CircuitBreakerRegistry.get(name)
        if existing is not None:
            return existing

        # Create a new instance
        instance = super().__call__(name, **kwargs)
        CircuitBreakerRegistry.register(instance)
        return instance


class CircuitBreaker(metaclass=_CircuitBreakerMeta):
    """Circuit Breaker.

    This class implements the circuit breaker pattern to prevent cascading failures
    by monitoring the number of consecutive errors in protected code blocks.

    Usage:
        # Create a circuit breaker instance
        cb = CircuitBreaker("my-circuit")

        # Option 1: As a context manager
        with cb.guard(exclude_errors=(ValueError,)):
            # protected code here

        # Option 2: As a decorator
        @cb.protect(exclude_errors=(ValueError,))
        def my_function():
            pass

        # Option 3: As a function wrapper
        result = cb.call(some_function, arg1, arg2, exclude_errors=(ValueError,))

    Raises:
        CircuitBreakerError: If the circuit is OPEN or call is denied in HALF_OPEN state.
    """

    def __init__(
        self,
        name: str,
        *,
        error_threshold: int = 5,
        success_threshold: int = 2,
        half_open_max_duration: float = 0.1,
        half_open_concurrent_calls: int = 1,
        logger: Logger | None = None,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            name: Name of the circuit breaker instance.
            error_threshold: Number of errors before opening the circuit.
            success_threshold: Number of successes before closing the circuit.
            half_open_max_duration: Time to wait before transitioning to half-open.
            half_open_concurrent_calls: Concurrent calls allowed in half-open state.
            logger: Logger for logging events, defaults to `grelmicro.circuitbreaker.{name}`.
        """
        # Public configuration
        self.error_threshold = error_threshold
        self.success_threshold = success_threshold
        self.half_open_max_duration = half_open_max_duration
        self.half_open_concurrent_calls = half_open_concurrent_calls

        self.logger = logger or getLogger(f"grelmicro.circuitbreaker.{name}")

        # Private state
        self._name = name
        self._state = CircuitBreakerState.CLOSED
        self._error_count = 0
        self._success_count = 0
        self._last_error: ErrorInfo | None = None
        self._open_until = 0.0
        self._active_calls = 0

    @property
    def name(self) -> str:
        """Return the name of the circuit breaker."""
        return self._name

    @property
    def state(self) -> CircuitBreakerState:
        """Return the current state of the circuit breaker."""
        self._try_transition_to_half_open()
        return self._state

    @property
    def last_error(self) -> ErrorInfo | None:
        """Return the last error that caused the circuit to open, if any."""
        return self._last_error

    def statistics(self) -> CircuitBreakerStatistics:
        """Return current statistics for this circuit breaker."""
        return CircuitBreakerStatistics(
            name=self._name,
            state=self.state,
            active_calls=self._active_calls,
            error_count=self._error_count,
            success_count=self._success_count,
            last_error=self._last_error,
        )

    @asynccontextmanager
    async def guard(
        self, *, ignore_errors: _ErrorTypes = ()
    ) -> AsyncGenerator[None, None]:
        """Guard a block of code with circuit breaker protection.

        This method allows you to execute a block of code while respecting the circuit breaker
        state.

        Args:
            ignore_errors: Exceptions to not count as errors for circuit breaker

        Returns:
            A context manager that yields None

        Raises:
            CircuitBreakerError: If the circuit is open or access is denied.
        """
        if not self._is_call_permitted():
            raise CircuitBreakerError(last_error=self._last_error)
        try:
            self._active_calls += 1
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
            self._active_calls -= 1

    def _is_call_permitted(self) -> bool:
        """Raise CircuitBreakerError if the call is not permitted."""
        if self._state is CircuitBreakerState.CLOSED:
            return True
        self._try_transition_to_half_open()
        return bool(
            self._state is CircuitBreakerState.HALF_OPEN
            and self._active_calls < self.half_open_concurrent_calls
        )

    def _on_error(self, error: Exception) -> None:
        """On error, increment error count and possibly open circuit."""
        self._success_count = 0
        self._error_count += 1
        self._last_error = ErrorInfo(timestamp=datetime.now(UTC), error=error)
        self._try_transition_to_open()

    def _on_success(self) -> None:
        """Reset error count and close circuit."""
        self._success_count += 1
        self._error_count = 0
        self._try_transition_to_closed()

    def _try_transition_to_closed(self) -> None:
        """Transition the circuit breaker to closed state if success threshold is met."""
        if (
            self._state is CircuitBreakerState.HALF_OPEN
            and self._success_count >= self.success_threshold
        ):
            self._state = CircuitBreakerState.CLOSED
            self.logger.info("Circuit breaker '%s' closed", self._name)

    def _try_transition_to_half_open(self) -> None:
        """Transition the circuit breaker to half-open state if the open period has ended."""
        if (
            self._state is CircuitBreakerState.OPEN
            and monotonic() > self._open_until
        ):
            self._state = CircuitBreakerState.HALF_OPEN
            self._success_count = 0
            self._error_count = 0
            self.logger.info("Circuit breaker '%s' half-open", self._name)

    def _try_transition_to_open(self) -> None:
        """Transition the circuit breaker to open state."""
        if (
            self._state
            in (CircuitBreakerState.CLOSED, CircuitBreakerState.HALF_OPEN)
            and self._error_count >= self.error_threshold
        ):
            self._state = CircuitBreakerState.OPEN
            self._open_until = monotonic() + self.half_open_max_duration
            self.logger.error(
                "Circuit breaker '%s' opened after %d consecutive errors",
                self._name,
                self.error_threshold,
            )
