"""Outcome of a protected call.

Resilience strategies (Retry, Fallback, CircuitBreaker, ...) wrap a
call and observe what comes out: an exception or a return value.
The [`Outcome`][grelmicro.resilience.Outcome] container exposes
both sides through one shape so a single
[`Match`][grelmicro.resilience.Match] predicate can reason over
either.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Outcome[T]:
    """Result of a protected call: an exception or a return value.

    Exactly one of ``exception`` and ``result`` is meaningful per
    instance, signalled by ``raised``. The ``T`` type parameter is
    the function's return type.

    Read more in the [Retry filtering](../resilience/retry.md#filtering-outcomes-with-match) docs.
    """

    exception: Exception | None
    """The exception raised by the call, or ``None`` when it returned."""

    result: T | None
    """The value returned by the call, or ``None`` when it raised."""

    raised: bool
    """``True`` when the call raised, ``False`` when it returned."""

    @classmethod
    def from_exception(cls, exception: Exception) -> "Outcome[T]":
        """Build an Outcome for a raised call."""
        return cls(exception=exception, result=None, raised=True)

    @classmethod
    def from_result(cls, result: T) -> "Outcome[T]":
        """Build an Outcome for a returned call."""
        return cls(exception=None, result=result, raised=False)
