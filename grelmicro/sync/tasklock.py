"""grelmicro Task Lock.

A distributed lock for scheduled tasks with two time boundaries:
- min_lock_seconds: Prevents re-execution on other nodes after task completes.
- max_lock_seconds: Auto-expires the lock (deadlock protection).
"""

from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import Annotated, Self
from uuid import UUID

from anyio import WouldBlock, from_thread
from pydantic import BaseModel, model_validator
from typing_extensions import Doc

from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync._utils import (
    generate_task_token,
    generate_thread_token,
    generate_worker_id,
)
from grelmicro.sync.abc import Seconds, SyncBackend, Synchronization
from grelmicro.sync.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockReentrantError,
    LockReleaseError,
)

logger = getLogger("grelmicro.sync")


class TaskLockConfig(BaseModel, frozen=True, extra="forbid"):
    """Task Lock Config."""

    name: Annotated[
        str,
        Doc("""The name of the resource to lock."""),
    ]
    worker: Annotated[
        str | UUID,
        Doc("""The worker identity."""),
    ]
    min_lock_seconds: Annotated[
        Seconds,
        Doc(
            """
            The minimum duration in seconds to hold the lock after task completion.

            Prevents re-execution on other nodes before this duration has elapsed.
            """
        ),
    ]
    max_lock_seconds: Annotated[
        Seconds,
        Doc(
            """
            The maximum duration in seconds to hold the lock (deadlock protection).

            Acts as the TTL on acquire.
            """
        ),
    ]

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.min_lock_seconds > self.max_lock_seconds:
            msg = "min_lock_seconds must be less than or equal to max_lock_seconds"
            raise ValueError(msg)
        return self


class TaskLock(Synchronization):
    """Task Lock.

    A distributed lock for scheduled tasks. Unlike a regular Lock,
    TaskLock does not release immediately on context manager exit. Instead, it keeps
    the lock held for at least `min_lock_seconds` seconds to prevent re-execution
    on other nodes.

    There is no background task that maintains the lock active during execution.
    The lock relies entirely on the TTL (`max_lock_seconds`) set at acquire time.

    This lock is designed to be used as the `sync` parameter of `IntervalTask`.
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                """
                The name of the resource to lock.

                It will be used as the lock name so make sure it is unique on the lock backend.
                """
            ),
        ],
        *,
        backend: Annotated[
            SyncBackend | None,
            Doc(
                """
                The distributed lock backend used to acquire and release the lock.

                By default, it will use the lock backend registry to get the default lock backend.
                """
            ),
        ] = None,
        worker: Annotated[
            str | UUID | None,
            Doc(
                """
                The worker identity.

                By default, a UUIDv1 will be generated.
                """
            ),
        ] = None,
        min_lock_seconds: Annotated[
            Seconds,
            Doc(
                """
                The minimum duration in seconds to hold the lock after task completion.

                Prevents re-execution on other nodes before this duration has elapsed.
                """
            ),
        ] = 1,
        max_lock_seconds: Annotated[
            Seconds,
            Doc(
                """
                The maximum duration in seconds to hold the lock (deadlock protection).

                Acts as the TTL on acquire.
                """
            ),
        ] = 60,
    ) -> None:
        """Initialize the task lock."""
        self._config = TaskLockConfig(
            name=name,
            worker=worker or generate_worker_id(),
            min_lock_seconds=min_lock_seconds,
            max_lock_seconds=max_lock_seconds,
        )
        self._backend = backend or get_sync_backend()
        self._acquired_at: float | None = None
        self._from_thread: ThreadTaskLockAdapter | None = None

    async def __aenter__(self) -> Self:
        """Acquire the lock with duration=max_lock_seconds.

        Raises:
            WouldBlock: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._config.name)

        token = generate_task_token(self._config.worker)
        if not await self.do_acquire(token):
            msg = f"Task lock not acquired: name={self._config.name}, token={token}"
            raise WouldBlock(msg)

        self.mark_acquired()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Release or extend the lock based on elapsed time.

        If elapsed >= min_lock_seconds, release immediately.
        If elapsed < min_lock_seconds, re-acquire with remaining duration (let TTL expire).

        Raises:
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        token = generate_task_token(self._config.worker)
        await self.do_exit(token)

    @property
    def config(self) -> TaskLockConfig:
        """Return the task lock config."""
        return self._config

    @property
    def from_thread(self) -> "ThreadTaskLockAdapter":
        """Return the task lock adapter for worker thread."""
        if self._from_thread is None:
            self._from_thread = ThreadTaskLockAdapter(task_lock=self)
        return self._from_thread

    async def locked(self) -> bool:
        """Check if the lock is acquired.

        Raises:
            LockLockedCheckError: If the lock cannot be checked due to an error on the backend.
        """
        try:
            return await self._backend.locked(name=self._config.name)
        except Exception as exc:
            raise LockLockedCheckError(name=self._config.name) from exc

    async def do_acquire(self, token: str) -> bool:
        """Acquire the lock.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was acquired, False if the lock was not acquired.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        try:
            return await self._backend.acquire(
                name=self._config.name,
                token=token,
                duration=self._config.max_lock_seconds,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._config.name, token=token) from exc

    async def do_release(self, token: str) -> bool:
        """Release the lock.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was released, False otherwise.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
        """
        try:
            return await self._backend.release(
                name=self._config.name, token=token
            )
        except Exception as exc:
            raise LockReleaseError(name=self._config.name, token=token) from exc

    async def do_reacquire(self, token: str, duration: float) -> bool:
        """Re-acquire the lock with a specific duration.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was re-acquired, False otherwise.

        Raises:
            LockReleaseError: Cannot re-acquire the lock due to backend error.
        """
        try:
            return await self._backend.acquire(
                name=self._config.name,
                token=token,
                duration=duration,
            )
        except Exception as exc:
            raise LockReleaseError(name=self._config.name, token=token) from exc

    async def do_exit(self, token: str) -> None:
        """Handle exit logic: release or re-acquire based on elapsed time."""
        elapsed = monotonic() - self._acquired_at  # type: ignore[operator]

        if elapsed >= self._config.min_lock_seconds:
            # Task took longer than min_lock_seconds, release immediately
            released = await self.do_release(token)
            if not released:
                logger.warning(
                    "Task lock expired before release"
                    " (elapsed: %.1fs, max_lock_seconds: %.1fs): %s",
                    elapsed,
                    self._config.max_lock_seconds,
                    self._config.name,
                )
        else:
            # Re-acquire with remaining duration to keep lock held
            remaining = self._config.min_lock_seconds - elapsed
            re_acquired = await self.do_reacquire(token, remaining)
            if not re_acquired:
                logger.warning(
                    "Task lock lost before re-acquire"
                    " (elapsed: %.1fs, min_lock_seconds: %.1fs): %s",
                    elapsed,
                    self._config.min_lock_seconds,
                    self._config.name,
                )

        self._acquired_at = None

    def mark_acquired(self) -> None:
        """Record the acquisition timestamp."""
        self._acquired_at = monotonic()


class ThreadTaskLockAdapter:
    """Task Lock Adapter for Worker Thread."""

    def __init__(self, task_lock: TaskLock) -> None:
        """Initialize the task lock adapter."""
        self._task_lock = task_lock

    def __enter__(self) -> Self:
        """Acquire the task lock with the context manager.

        Raises:
            WouldBlock: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        if self._task_lock._acquired_at is not None:  # noqa: SLF001
            raise LockReentrantError(name=self._task_lock.config.name)

        token = generate_thread_token(self._task_lock.config.worker)
        if not from_thread.run(self._task_lock.do_acquire, token):
            msg = (
                f"Task lock not acquired:"
                f" name={self._task_lock.config.name}, token={token}"
            )
            raise WouldBlock(msg)

        self._task_lock.mark_acquired()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release or extend the lock based on elapsed time.

        Raises:
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        token = generate_thread_token(self._task_lock.config.worker)
        from_thread.run(self._task_lock.do_exit, token)

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return from_thread.run(self._task_lock.locked)
