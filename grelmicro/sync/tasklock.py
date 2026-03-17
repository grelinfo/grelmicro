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

from anyio import WouldBlock
from pydantic import BaseModel, model_validator
from typing_extensions import Doc

from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync._utils import generate_task_token, generate_worker_id
from grelmicro.sync.abc import Seconds, SyncBackend, Synchronization
from grelmicro.sync.errors import LockAcquireError, LockReleaseError

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

    async def __aenter__(self) -> Self:
        """Acquire the lock with duration=max_lock_seconds.

        Raises:
            WouldBlock: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
        """
        token = generate_task_token(self._config.worker)
        try:
            acquired = await self._backend.acquire(
                name=self._config.name,
                token=token,
                duration=self._config.max_lock_seconds,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._config.name, token=token) from exc

        if not acquired:
            msg = f"Task lock not acquired: name={self._config.name}, token={token}"
            raise WouldBlock(msg)

        self._acquired_at = monotonic()
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
        elapsed = monotonic() - self._acquired_at  # type: ignore[operator]

        if elapsed >= self._config.min_lock_seconds:
            # Task took longer than min_lock_seconds, release immediately
            try:
                released = await self._backend.release(
                    name=self._config.name, token=token
                )
            except Exception as exc:
                raise LockReleaseError(
                    name=self._config.name, token=token
                ) from exc
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
            try:
                re_acquired = await self._backend.acquire(
                    name=self._config.name,
                    token=token,
                    duration=remaining,
                )
            except Exception as exc:
                raise LockReleaseError(
                    name=self._config.name, token=token
                ) from exc
            if not re_acquired:
                logger.warning(
                    "Task lock lost before re-acquire"
                    " (elapsed: %.1fs, min_lock_seconds: %.1fs): %s",
                    elapsed,
                    self._config.min_lock_seconds,
                    self._config.name,
                )

        self._acquired_at = None
