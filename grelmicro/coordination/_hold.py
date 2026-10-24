"""Hold a lock for a block, waiting at most a given time to acquire it."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from grelmicro._async import run_to_completion
from grelmicro.coordination._handle import LockHandle
from grelmicro.coordination.errors import LockNotOwnedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from grelmicro.coordination._protocol import Seconds

_LEASE_LOST = "lock lease lost"
"""The message on the cancellation a renewed hold sends its body."""


class _Acquire[H](Protocol):
    """An acquire that waits at most `timeout` seconds."""

    def __call__(self, *, timeout: Seconds | None) -> Awaitable[H]: ...


class _ThreadAcquire[H](Protocol):
    """A blocking acquire that waits at most `timeout` seconds."""

    def __call__(self, *, timeout: Seconds | None) -> H: ...


class Hold[H]:
    """The lock held for the body of `async with`.

    Entering waits at most `timeout` seconds to acquire the lock, then
    yields what the acquire returns. Leaving releases it, whether the body
    ended or raised, exactly as leaving `async with lock:` does. It is not
    awaitable: use it with `async with` only.
    """

    __slots__ = ("_acquire", "_exit", "_timeout")

    def __init__(
        self,
        acquire: _Acquire[H],
        exit_: Callable[
            [
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ],
            Awaitable[bool | None],
        ],
        timeout: Seconds | None,
    ) -> None:
        """Enter through `acquire`, leave through `exit_`, the lock's own exit."""
        self._acquire = acquire
        self._exit = exit_
        self._timeout = timeout

    async def __aenter__(self) -> H:
        """Acquire the lock, waiting at most `timeout` seconds.

        Raises:
            LockTimeoutError: If `timeout` elapsed before the lock was
                acquired.
            LockReentrantError: If this task or thread already holds it.
            LockAcquireError: If the backend call failed.
        """
        return await self._acquire(timeout=self._timeout)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lease ran out during the body.
            LockReleaseError: If the backend call failed.
        """
        return await self._exit(exc_type, exc_value, traceback)


class RenewedHold(Hold[LockHandle]):
    """The lock held for the body of `async with`, its lease kept alive.

    Entering acquires the lock as `Hold` does, then renews the lease from a
    task of its own until the body ends. When the lease is lost, the body
    is cancelled at its next `await` and leaving raises `LockNotOwnedError`.
    A cancellation from outside is raised as it is.
    """

    __slots__ = (
        "_cancelling",
        "_drop",
        "_keep",
        "_lost",
        "_name",
        "_renewer",
        "_revoked",
        "_stop",
        "_task",
    )

    _task: asyncio.Task[object]
    _renewer: asyncio.Task[None]

    def __init__(
        self,
        acquire: _Acquire[LockHandle],
        exit_: Callable[
            [
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ],
            Awaitable[bool | None],
        ],
        timeout: Seconds | None,
        *,
        name: str,
        keep: Callable[[str, asyncio.Event], Awaitable[bool]],
        drop: Callable[[], Awaitable[None]],
    ) -> None:
        """Renew through `keep`, and leave a lost lease through `drop`.

        `keep(token, stop)` extends the lease until `stop` is set and
        answers whether the lease was lost. `drop()` lets go of a lost
        lease in place of `exit_`.
        """
        super().__init__(acquire, exit_, timeout)
        self._name = name
        self._keep = keep
        self._drop = drop
        self._stop = asyncio.Event()
        self._lost = False
        self._revoked = False
        self._cancelling = 0

    async def __aenter__(self) -> LockHandle:
        """Acquire the lock, then start renewing its lease.

        Raises:
            LockTimeoutError: If `timeout` elapsed before the lock was
                acquired.
            LockReentrantError: If this task already holds it.
            LockAcquireError: If the backend call failed.
        """
        held = await self._acquire(timeout=self._timeout)
        task = asyncio.current_task()
        if task is None:  # pragma: no cover
            msg = "Lock async APIs must be called from a running asyncio task"
            raise RuntimeError(msg)
        self._task = task
        self._cancelling = task.cancelling()
        self._renewer = asyncio.create_task(
            self._renew(held.token), name=f"grelmicro-lock-renew:{self._name}"
        )
        return held

    async def _renew(self, token: str) -> None:
        """Keep the lease, and cancel the body once when it is lost."""
        if not await self._keep(token, self._stop):
            return
        self._lost = True
        if not self._stop.is_set():
            self._revoked = True
            self._task.cancel(_LEASE_LOST)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Stop the renewals, then release the lock.

        Raises:
            LockNotOwnedError: If the lease was lost during the body.
            LockReleaseError: If the backend call failed.
        """
        self._stop.set()
        own_cancel = False
        if self._revoked:
            own_cancel = self._task.uncancel() <= self._cancelling
        try:
            await run_to_completion(asyncio.wait((self._renewer,)))
        finally:
            if self._lost:
                await self._drop()
            else:
                await self._exit(exc_type, exc_value, traceback)
        if self._lost and (
            exc_type is None
            or (own_cancel and issubclass(exc_type, asyncio.CancelledError))
        ):
            raise LockNotOwnedError(name=self._name) from exc_value
        return None


class ThreadHold[H]:
    """The lock held for the body of `with`, from a worker thread.

    Entering blocks the thread at most `timeout` seconds to acquire the
    lock, then yields what the acquire returns. Leaving releases it,
    exactly as leaving `with lock.from_thread:` does.
    """

    __slots__ = ("_acquire", "_exit", "_timeout")

    def __init__(
        self,
        acquire: _ThreadAcquire[H],
        exit_: Callable[
            [
                type[BaseException] | None,
                BaseException | None,
                TracebackType | None,
            ],
            bool | None,
        ],
        timeout: Seconds | None,
    ) -> None:
        """Enter through `acquire`, leave through `exit_`, the lock's own exit."""
        self._acquire = acquire
        self._exit = exit_
        self._timeout = timeout

    def __enter__(self) -> H:
        """Acquire the lock, blocking at most `timeout` seconds.

        Raises:
            LockTimeoutError: If `timeout` elapsed before the lock was
                acquired.
            LockReentrantError: If this task or thread already holds it.
            LockAcquireError: If the backend call failed.
        """
        return self._acquire(timeout=self._timeout)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lease ran out during the body.
            LockReleaseError: If the backend call failed.
        """
        return self._exit(exc_type, exc_value, traceback)
