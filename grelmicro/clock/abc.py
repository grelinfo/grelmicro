"""Clock Abstract Base Classes and Protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from types import TracebackType


@runtime_checkable
class ClockBackend(Protocol):
    """Clock Backend Protocol.

    Owns the two time operations grelmicro primitives depend on: reading a
    monotonic clock and sleeping. The default `RealClock` forwards to
    `time.monotonic` and `asyncio.sleep`. A `VirtualClock` replaces both with
    a manually advanced virtual timeline so time-dependent code runs without
    real waiting in tests.
    """

    async def __aenter__(self) -> Self:
        """Install this clock for the surrounding scope."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Restore the previously active clock."""
        ...

    def monotonic(self) -> float:
        """Return the current value of a monotonic clock, in seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the current task for `seconds` of this clock's time."""
        ...
