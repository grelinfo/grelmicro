"""Lock."""

from threading import get_ident
from types import TracebackType
from typing import Annotated, Self
from uuid import UUID

from anyio import WouldBlock, from_thread, get_current_task, sleep
from pydantic import model_validator
from typing_extensions import Doc

from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync._base import BaseLock, BaseLockConfig
from grelmicro.sync._tokens import (
    generate_task_token,
    generate_worker_id,
)
from grelmicro.sync.abc import Seconds, SyncBackend
from grelmicro.sync.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
)

_MIN_RETRY_INTERVAL: float = 0.001


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

            Must be >= 0.001 to prevent flooding the lock backend.
            """,
        ),
    ]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.retry_interval < _MIN_RETRY_INTERVAL:
            msg = f"retry_interval must be >= {_MIN_RETRY_INTERVAL}"
            raise ValueError(msg)
        return self


class Lock(BaseLock):
    """Lock.

    This lock is a distributed lock that is used to acquire a resource across multiple workers. The
    lock is acquired asynchronously and can be extended multiple times manually. The lock is
    automatically released after a duration if not extended.
    """

    _LOCK_PREFIX = "lock"

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

                By default, a UUIDv1 will be generated.
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
            worker=worker or generate_worker_id(),
            lease_duration=lease_duration,
            retry_interval=retry_interval,
        )
        self._lock_name = f"{self._LOCK_PREFIX}:{name}"
        self.backend = backend or get_sync_backend()
        self._held_by_tasks: set[int] = set()
        self._held_by_threads: set[int] = set()
        self._from_thread: ThreadLockAdapter | None = None

    async def __aenter__(self) -> Self:
        """Acquire the lock with the async context manager.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
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
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        task_id = get_current_task().id
        if task_id in self._held_by_tasks:
            raise LockReentrantError(name=self._config.name)
        token = generate_task_token(self._config.worker)
        while not await self.do_acquire(token=token):  # noqa: ASYNC110 // Polling is intentional
            await sleep(self._config.retry_interval)
        self._held_by_tasks.add(task_id)

    async def acquire_nowait(self) -> None:
        """
        Acquire the lock, without blocking.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            WouldBlock: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        task_id = get_current_task().id
        if task_id in self._held_by_tasks:
            raise LockReentrantError(name=self._config.name)
        token = generate_task_token(self._config.worker)
        if not await self.do_acquire(token=token):
            msg = f"Lock not acquired: name={self._config.name}, token={token}"
            raise WouldBlock(msg)
        self._held_by_tasks.add(task_id)

    async def release(self) -> None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.

        """
        self._held_by_tasks.discard(get_current_task().id)
        token = generate_task_token(self._config.worker)
        if not await self.do_release(token):
            raise LockNotOwnedError(name=self._config.name)

    async def locked(self) -> bool:
        """Check if the lock is acquired.

        Raises:
            LockLockedCheckError: If the lock cannot be checked due to an error on the backend.
        """
        try:
            return await self.backend.locked(name=self._lock_name)
        except Exception as exc:
            raise LockLockedCheckError(name=self._config.name) from exc

    async def owned(self) -> bool:
        """Check if the lock is owned by the current token.

        Raises:
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
                name=self._lock_name,
                token=token,
                duration=self._config.lease_duration,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._config.name) from exc

    async def do_release(self, token: str) -> bool:
        """Release the lock.

        This method should not be called directly. Use `release` instead.

        Returns:
            bool: True if the lock was released, False otherwise.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
        """
        try:
            return await self.backend.release(name=self._lock_name, token=token)
        except Exception as exc:
            raise LockReleaseError(name=self._config.name) from exc

    async def do_owned(self, token: str) -> bool:
        """Check if the lock is owned by the current token.

        This method should not be called directly. Use `owned` instead.

        Returns:
            bool: True if the lock is owned by the current token, False otherwise.

        Raises:
            LockOwnedCheckError: Cannot check if the lock is owned due to backend error.
        """
        try:
            return await self.backend.owned(name=self._lock_name, token=token)
        except Exception as exc:
            raise LockOwnedCheckError(name=self._config.name) from exc

    def _thread_token(self, thread_id: int) -> str:
        """Build a thread token from the worker identity and the given thread ID."""
        return f"{self._config.worker}:thread:{thread_id}"

    async def do_thread_acquire(self, thread_id: int) -> None:
        """Acquire the lock from a worker thread (blocking).

        Runs on the event loop so the reentrant check and backend acquire are
        atomic with respect to other threads.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        if thread_id in self._held_by_threads:
            raise LockReentrantError(name=self._config.name)
        token = self._thread_token(thread_id)
        while not await self.do_acquire(token=token):  # noqa: ASYNC110 // Polling is intentional
            await sleep(self._config.retry_interval)
        self._held_by_threads.add(thread_id)

    async def do_thread_acquire_nowait(self, thread_id: int) -> None:
        """Acquire the lock from a worker thread (non-blocking).

        Runs on the event loop so the reentrant check and backend acquire are
        atomic with respect to other threads.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            WouldBlock: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        if thread_id in self._held_by_threads:
            raise LockReentrantError(name=self._config.name)
        token = self._thread_token(thread_id)
        if not await self.do_acquire(token=token):
            msg = f"Lock not acquired: name={self._config.name}, token={token}"
            raise WouldBlock(msg)
        self._held_by_threads.add(thread_id)

    async def do_thread_release(self, thread_id: int) -> None:
        """Release the lock from a worker thread.

        Runs on the event loop so the backend release is atomic with respect
        to other threads.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.
        """
        self._held_by_threads.discard(thread_id)
        token = self._thread_token(thread_id)
        if not await self.do_release(token):
            raise LockNotOwnedError(name=self._config.name)


class ThreadLockAdapter:
    """Lock Adapter for Worker Thread."""

    def __init__(self, lock: Lock) -> None:
        """Initialize the lock adapter."""
        self._lock = lock

    def __enter__(self) -> Self:
        """Acquire the lock with the context manager.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.
        """
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
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.

        """
        from_thread.run(self._lock.do_thread_acquire, get_ident())

    def acquire_nowait(self) -> None:
        """
        Acquire the lock, without blocking.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.
            WouldBlock: If the lock cannot be acquired without blocking.

        """
        from_thread.run(self._lock.do_thread_acquire_nowait, get_ident())

    def release(self) -> None:
        """Release the lock.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
            LockNotOwnedError: If the lock is not currently held.

        """
        from_thread.run(self._lock.do_thread_release, get_ident())

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return from_thread.run(self._lock.locked)

    def owned(self) -> bool:
        """Return True if the lock is currently held by the current worker thread."""
        return from_thread.run(
            self._lock.do_owned,
            self._lock._thread_token(get_ident()),  # noqa: SLF001
        )
