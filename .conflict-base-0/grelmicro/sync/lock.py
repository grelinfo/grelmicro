"""Grelmicro Lock."""

from time import sleep as thread_sleep
from types import TracebackType
from typing import Annotated, Self
from uuid import UUID, uuid1

from anyio import WouldBlock, from_thread, sleep
from typing_extensions import Doc

from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync._base import BaseLock, BaseLockConfig
from grelmicro.sync._utils import generate_task_token, generate_thread_token
from grelmicro.sync.abc import Seconds, SyncBackend
from grelmicro.sync.errors import (
    LockAcquireError,
    LockNotOwnedError,
    LockReleaseError,
    SyncBackendError,
)


class LockConfig(BaseLockConfig, frozen=True, extra="forbid"):
    """Lock Config."""

    lease_duration: Annotated[
        Seconds,
        Doc(
            """
            The lease duration in seconds for the lock.
            """,
        ),
    ]
    retry_interval: Annotated[
        Seconds,
        Doc(
            """
            The interval in seconds between attempts to acquire the lock.
            """,
        ),
    ]


class Lock(BaseLock):
    """Lock.

    This lock is a distributed lock that is used to acquire a resource across multiple workers. The
    lock is acquired asynchronously and can be extended multiple times manually. The lock is
    automatically released after a duration if not extended.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                """
                The name of the resource to lock.

                It will be used as the lock name so make sure it is unique on the lock backend.
                """,
            ),
        ],
        *,
        backend: Annotated[
            SyncBackend | None,
            Doc("""
                The distributed lock backend used to acquire and release the lock.

                By default, it will use the lock backend registry to get the default lock backend.
                """),
        ] = None,
        worker: Annotated[
            str | UUID | None,
            Doc(
                """
                The worker identity.

                By default, use a UUIDv1 will be generated.
                """,
            ),
        ] = None,
        lease_duration: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds for the lock to be held by default.
                """,
            ),
        ] = 60,
        retry_interval: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds between attempts to acquire the lock.

                Should be greater or equal than 0.1 to prevent flooding the lock backend.
                """,
            ),
        ] = 0.1,
    ) -> None:
        """Initialize the lock."""
        self._config: LockConfig = LockConfig(
            name=name,
            worker=worker or uuid1(),
            lease_duration=lease_duration,
            retry_interval=retry_interval,
        )
        self.backend = backend or get_sync_backend()
        self._from_thread: ThreadLockAdapter | None = None

    async def __aenter__(self) -> Self:
        """Acquire the lock with the async context manager."""
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock with the async context manager.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.

        """
        await self.release()

    @property
    def config(self) -> LockConfig:
        """Return the lock config."""
        return self._config

    @property
    def from_thread(self) -> "ThreadLockAdapter":
        """Return the lock adapter for worker thread."""
        if self._from_thread is None:
            self._from_thread = ThreadLockAdapter(lock=self)
        return self._from_thread

    async def acquire(self) -> None:
        """Acquire the lock.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        token = generate_task_token(self._config.worker)
        while not await self.do_acquire(token=token):  # noqa: ASYNC110 // Polling is intentional
            await sleep(self._config.retry_interval)

    async def acquire_nowait(self) -> None:
        """
        Acquire the lock, without blocking.

        Raises:
            WouldBlock: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        token = generate_task_token(self._config.worker)
        if not await self.do_acquire(token=token):
            msg = f"Lock not acquired: name={self._config.name}, token={token}"
            raise WouldBlock(msg)

    async def release(self) -> None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.

        """
        token = generate_task_token(self._config.worker)
        if not await self.do_release(token):
            raise LockNotOwnedError(name=self._config.name, token=token)

    async def locked(self) -> bool:
        """Check if the lock is acquired.

        Raise:
            SyncBackendError: If the lock cannot be checked due to an error on the backend.
        """
        try:
            return await self.backend.locked(name=self._config.name)
        except Exception as exc:
            msg = "Failed to check if the lock is acquired"
            raise SyncBackendError(msg) from exc

    async def owned(self) -> bool:
        """Check if the lock is owned by the current token.

        Raise:
            SyncBackendError: If the lock cannot be checked due to an error on the backend.
        """
        return await self.do_owned(generate_task_token(self._config.worker))

    async def do_acquire(self, token: str) -> bool:
        """Acquire the lock.

        This method should not be called directly. Use `acquire` instead.

        Returns:
            bool: True if the lock was acquired, False if the lock was not acquired.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        try:
            return await self.backend.acquire(
                name=self._config.name,
                token=token,
                duration=self._config.lease_duration,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._config.name, token=token) from exc

    async def do_release(self, token: str) -> bool:
        """Release the lock.

        This method should not be called directly. Use `release` instead.

        Returns:
            bool: True if the lock was released, False otherwise.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
        """
        try:
            return await self.backend.release(
                name=self._config.name, token=token
            )
        except Exception as exc:
            raise LockReleaseError(name=self._config.name, token=token) from exc

    async def do_owned(self, token: str) -> bool:
        """Check if the lock is owned by the current token.

        This method should not be called directly. Use `owned` instead.

        Returns:
            bool: True if the lock is owned by the current token, False otherwise.

        Raises:
            SyncBackendError: Cannot check if the lock is owned due to backend error.
        """
        try:
            return await self.backend.owned(name=self._config.name, token=token)
        except Exception as exc:
            msg = "Failed to check if the lock is owned"
            raise SyncBackendError(msg) from exc


class ThreadLockAdapter:
    """Lock Adapter for Worker Thread."""

    def __init__(self, lock: Lock) -> None:
        """Initialize the lock adapter."""
        self._lock = lock

    def __enter__(self) -> Self:
        """Acquire the lock with the context manager."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock with the context manager."""
        self.release()

    def acquire(self) -> None:
        """Acquire the lock.

        Raises:
            LockAcquireError: Cannot acquire the lock due to backend error.

        """
        token = generate_thread_token(self._lock.config.worker)
        retry_interval = self._lock.config.retry_interval
        while not from_thread.run(self._lock.do_acquire, token):
            thread_sleep(retry_interval)

    def acquire_nowait(self) -> None:
        """
        Acquire the lock, without blocking.

        Raises:
            LockAcquireError: Cannot acquire the lock due to backend error.
            WouldBlock: If the lock cannot be acquired without blocking.

        """
        token = generate_thread_token(self._lock.config.worker)
        if not from_thread.run(self._lock.do_acquire, token):
            msg = f"Lock not acquired: name={self._lock.config.name}, token={token}"
            raise WouldBlock(msg)

    def release(self) -> None:
        """Release the lock.

        Raises:
            ReleaseSyncBackendError: Cannot release the lock due to backend error.
            LockNotOwnedError: If the lock is not currently held.

        """
        token = generate_thread_token(self._lock.config.worker)
        if not from_thread.run(self._lock.do_release, token):
            raise LockNotOwnedError(name=self._lock.config.name, token=token)

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return from_thread.run(self._lock.locked)

    def owned(self) -> bool:
        """Return True if the lock is currently held by the current worker thread."""
        return from_thread.run(
            self._lock.do_owned, generate_thread_token(self._lock.config.worker)
        )
