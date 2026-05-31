"""Reusable async fault-injection helpers for tests.

Small, typed building blocks that simulate the failure modes a
distributed backend hits in production: added latency, transient
errors that clear on retry, dropped connections, and tasks cancelled
mid-flight. Import them directly:

```python
from tests._faults import flaky, connection_drop
```
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class TransientError(RuntimeError):
    """A short-lived failure that clears on retry."""


class ConnectionDroppedError(ConnectionError):
    """A dropped backend connection."""


async def delayed[T](
    coro_fn: Callable[[], Awaitable[T]],
    seconds: float,
) -> T:
    """Await ``coro_fn`` after injecting ``seconds`` of latency.

    Use a small value so the test stays fast. The delay runs before
    the wrapped call so the call still observes the latency.
    """
    await asyncio.sleep(seconds)
    return await coro_fn()


class flaky[T]:  # noqa: N801
    """Wrap a coroutine factory so it fails the first ``failures`` calls.

    Each call raises ``error`` until ``failures`` calls have been made,
    then every later call delegates to ``coro_fn`` and returns its
    result. Drives retry and circuit-breaker recovery paths.

    Example:
    ```python
    call = flaky(backend.ping, failures=2)
    await call()  # raises TransientError
    await call()  # raises TransientError
    await call()  # succeeds
    ```
    """

    def __init__(
        self,
        coro_fn: Callable[[], Awaitable[T]],
        *,
        failures: int = 1,
        error: BaseException | None = None,
    ) -> None:
        """Store the wrapped factory and the failure budget."""
        self._coro_fn = coro_fn
        self._failures = failures
        self._error = error or TransientError("transient failure")
        self.calls = 0

    async def __call__(self) -> T:
        """Raise while inside the failure budget, then delegate."""
        self.calls += 1
        if self.calls <= self._failures:
            raise self._error
        return await self._coro_fn()


async def connection_drop() -> None:
    """Raise a connection-style error to simulate a dropped backend call."""
    msg = "connection dropped"
    raise ConnectionDroppedError(msg)


async def cancel_midflight[T](
    coro_fn: Callable[[], Awaitable[T]],
    *,
    after: float = 0.0,
) -> asyncio.Task[T]:
    """Start ``coro_fn`` as a task and cancel it after ``after`` seconds.

    Returns the cancelled task. The task has been awaited to settle, so
    the caller can inspect it (for example assert ``task.cancelled()``).
    """
    task: asyncio.Task[T] = asyncio.ensure_future(coro_fn())
    await asyncio.sleep(after)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task
