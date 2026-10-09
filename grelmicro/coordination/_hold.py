"""Hold a lock for a block, waiting at most a given time to acquire it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from grelmicro.coordination._protocol import Seconds


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
