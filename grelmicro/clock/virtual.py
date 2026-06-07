"""Virtual clock backend for deterministic time in tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.clock._seam import _active_clock

if TYPE_CHECKING:
    from types import TracebackType


class VirtualClock:
    """A manually advanced clock for time-dependent tests.

    `monotonic()` returns virtual time and `sleep()` suspends until the virtual
    time is advanced past its deadline, so a retry budget, a breaker half-open
    window, or a rate limiter refill can be driven instantly instead of waiting
    real seconds.

    Register it on an app or install it directly, then advance time by hand:

    ```python
    clock = VirtualClock()
    micro = Grelmicro(uses=[clock, CircuitBreakers(...)])

    async with micro:
        await call()              # trips the breaker
        await clock.advance(30)   # half-open window elapses, no real wait
        await call()              # retried
    ```

    Time-dependent primitives read the active clock through grelmicro's time
    seam. Code that calls `asyncio.sleep` or `time.monotonic` directly is not
    affected.
    """

    kind: ClassVar[str] = "clock"

    def __init__(
        self,
        *,
        start: Annotated[
            float,
            Doc("Initial value of the virtual monotonic clock, in seconds."),
        ] = 0.0,
        name: Annotated[
            str,
            Doc("Registration name when used as a component."),
        ] = "default",
    ) -> None:
        """Initialize the virtual clock at `start`."""
        self._name = name
        self._now = start
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []
        self._token: Any = None

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    def monotonic(self) -> float:
        """Return the current virtual time."""
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Suspend until the virtual time is advanced past `seconds` from now."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        deadline = self._now + seconds
        future = asyncio.get_running_loop().create_future()
        waiter = (deadline, future)
        self._waiters.append(waiter)
        try:
            await future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def advance(
        self,
        seconds: Annotated[
            float,
            Doc("How far to move the virtual clock forward, in seconds."),
        ],
    ) -> None:
        """Move the clock forward and wake every sleeper whose deadline passed."""
        if seconds < 0:
            msg = "cannot advance the clock backwards"
            raise ValueError(msg)
        self._now += seconds
        remaining: list[tuple[float, asyncio.Future[None]]] = []
        for waiter in self._waiters:
            deadline, future = waiter
            if deadline <= self._now:
                if not future.done():
                    future.set_result(None)
            else:
                remaining.append(waiter)
        self._waiters = remaining
        # Let the woken tasks run their next step before returning.
        await asyncio.sleep(0)

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
