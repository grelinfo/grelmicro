"""Real (wall-clock) clock backend."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, ClassVar, Self

from grelmicro.clock._seam import _active_clock

if TYPE_CHECKING:
    from types import TracebackType


class RealClock:
    """The production clock: `time.monotonic` and `asyncio.sleep`.

    This is the default behavior even when no clock is registered. Register it
    explicitly (`Grelmicro(uses=[RealClock()])`) only to make the choice
    visible, or to shadow a `VirtualClock` in a nested scope.
    """

    kind: ClassVar[str] = "clock"

    def __init__(self, *, name: str = "default") -> None:
        """Initialize the clock."""
        self._name = name
        self._token: Any = None

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    def monotonic(self) -> float:
        """Return `time.monotonic()`."""
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """Sleep with `asyncio.sleep()`."""
        await asyncio.sleep(seconds)

    async def __aenter__(self) -> Self:
        """Install this clock for the surrounding scope."""
        self._token = _active_clock.set(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Restore the previously active clock."""
        _active_clock.reset(self._token)
