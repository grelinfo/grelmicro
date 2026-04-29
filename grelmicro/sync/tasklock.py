"""Task Lock.

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
from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._config import env_segment, resolve_config
from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync._base import BaseLockConfig
from grelmicro.sync._tokens import (
    generate_task_token,
    generate_thread_token,
    generate_token_nonce,
)
from grelmicro.sync.abc import Seconds, SyncBackend, SyncPrimitive
from grelmicro.sync.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockReentrantError,
    LockReleaseError,
)

logger = getLogger("grelmicro.sync")


class TaskLockConfig(BaseLockConfig, frozen=True, extra="forbid"):  # ty: ignore[invalid-frozen-dataclass-subclass]
    """Task Lock Config."""

    min_lock_seconds: Annotated[
        Seconds,
        Doc(
            """
            The minimum duration in seconds to hold the lock after task completion.

            Prevents re-execution on other nodes before this duration has elapsed.
            """
        ),
    ] = 1
    max_lock_seconds: Annotated[
        Seconds,
        Doc(
            """
            The maximum duration in seconds to hold the lock (deadlock protection).

            Acts as the TTL on acquire.
            """
        ),
    ] = 60

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.min_lock_seconds > self.max_lock_seconds:
            msg = "min_lock_seconds must be less than or equal to max_lock_seconds"
            raise ValueError(msg)
        return self


class TaskLock(SyncPrimitive):
    """Task Lock.

    A distributed lock for scheduled tasks. Unlike a regular Lock,
    TaskLock does not release immediately on context manager exit. Instead, it keeps
    the lock held for at least `min_lock_seconds` seconds to prevent re-execution
    on other nodes.

    There is no background task that maintains the lock active during execution.
    The lock relies entirely on the TTL (`max_lock_seconds`) set at acquire time.

    This lock is designed to be used as the `sync` parameter of `IntervalTask`.
    """

    _LOCK_PREFIX = "tasklock"

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

                By default, a UUIDv1 is generated.
                """
            ),
        ] = None,
        min_lock_seconds: Annotated[
            Seconds | None,
            Doc(
                """
                The minimum duration in seconds to hold the lock after task completion.

                Default: 1. Prevents re-execution on other nodes
                before this duration has elapsed. When unset,
                resolves from the environment variable
                `GREL_TASK_LOCK_{NAME_UPPER}_MIN_LOCK_SECONDS` if
                present, otherwise falls back to the
                `TaskLockConfig` default.
                """
            ),
        ] = None,
        max_lock_seconds: Annotated[
            Seconds | None,
            Doc(
                """
                The maximum duration in seconds to hold the lock (deadlock protection).

                Default: 60. Acts as the TTL on acquire. When unset,
                resolves from the environment variable
                `GREL_TASK_LOCK_{NAME_UPPER}_MAX_LOCK_SECONDS` if
                present, otherwise falls back to the
                `TaskLockConfig` default.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_TASK_LOCK_{NAME_UPPER}_`. Set this to
                a custom prefix when the application uses a different
                naming convention.
                """
            ),
        ] = None,
        read_env: Annotated[
            bool,
            Doc(
                """
                Whether to read environment variables.

                Default: True. Set to False when every field is
                already supplied via kwargs and the environment
                must not influence construction.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the task lock."""
        config = resolve_config(
            TaskLockConfig,
            explicit=None,
            kwargs={
                "worker": worker,
                "min_lock_seconds": min_lock_seconds,
                "max_lock_seconds": max_lock_seconds,
            },
            env_prefix=env_prefix or f"GREL_TASK_LOCK_{env_segment(name)}_",
            read_env=read_env,
        )
        self._setup(name, config, backend)

    @classmethod
    def from_config(
        cls,
        name: Annotated[
            str,
            Doc(
                """
                The name of the resource to lock.

                Acts as the instance identity. Used as the backend
                lock key and exposed via the `name` property.
                """
            ),
        ],
        config: Annotated[
            TaskLockConfig,
            Doc(
                """
                The pre-built task lock configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a `pydantic-settings` aggregator). The environment
                path is bypassed and the config is used as-is.
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
    ) -> Self:
        """Construct a `TaskLock` from a name and a pre-built `TaskLockConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: TaskLockConfig,
        backend: SyncBackend | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._lock_name = f"{self._LOCK_PREFIX}:{name}"
        self._backend: SyncBackend | None = backend
        self._acquired_at: float | None = None
        self._token_nonce = generate_token_nonce()
        self._from_thread: ThreadTaskLockAdapter | None = None

    @property
    def name(self) -> str:
        """Return the task lock identity."""
        return self._name

    @property
    def backend(self) -> SyncBackend:
        """Bound sync backend, resolved lazily on first access."""
        return self._backend or self._resolve_backend()

    def _resolve_backend(self) -> SyncBackend:
        """Resolve the backend from the global registry and cache it."""
        backend = get_sync_backend()
        self._backend = backend
        return backend

    async def __aenter__(self) -> Self:
        """Acquire the lock with duration=max_lock_seconds.

        Raises:
            WouldBlock: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._name)

        token = generate_task_token(self._config.worker, self._token_nonce)
        if not await self.do_acquire(token):
            msg = f"Task lock not acquired: name={self._name}, token={token}"
            raise WouldBlock(msg)

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Release or extend the lock based on elapsed time.

        If elapsed >= min_lock_seconds, release immediately.
        If elapsed < min_lock_seconds, re-acquire with remaining duration (let TTL expire).

        Raises:
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        token = generate_task_token(self._config.worker, self._token_nonce)
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
        backend = self._backend or self._resolve_backend()
        try:
            return await backend.locked(name=self._lock_name)
        except Exception as exc:
            raise LockLockedCheckError(name=self._name) from exc

    async def do_acquire(self, token: str) -> bool:
        """Acquire the lock.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was acquired, False if the lock was not acquired.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        backend = self._backend or self._resolve_backend()
        try:
            acquired = await backend.acquire(
                name=self._lock_name,
                token=token,
                duration=self._config.max_lock_seconds,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._name) from exc
        if acquired:
            self._acquired_at = monotonic()
        return acquired

    async def do_release(self, token: str) -> bool:
        """Release the lock.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was released, False otherwise.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
        """
        backend = self._backend or self._resolve_backend()
        try:
            return await backend.release(name=self._lock_name, token=token)
        except Exception as exc:
            raise LockReleaseError(name=self._name) from exc

    async def do_reacquire(self, token: str, duration: float) -> bool:
        """Re-acquire the lock with a specific duration.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was re-acquired, False otherwise.

        Raises:
            LockReleaseError: Cannot re-acquire the lock due to backend error.
        """
        backend = self._backend or self._resolve_backend()
        try:
            return await backend.acquire(
                name=self._lock_name,
                token=token,
                duration=duration,
            )
        except Exception as exc:
            raise LockReleaseError(name=self._name) from exc

    async def do_thread_enter(self) -> None:
        """Acquire the lock from a worker thread.

        Runs entirely on the event loop so the reentrant check, token
        generation, and backend acquire are atomic with respect to other
        threads.

        Raises:
            WouldBlock: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._name)

        token = generate_thread_token(self._config.worker, self._token_nonce)
        if not await self.do_acquire(token):
            msg = f"Task lock not acquired: name={self._name}, token={token}"
            raise WouldBlock(msg)

    async def do_thread_exit(self) -> None:
        """Release or extend the lock from a worker thread.

        Runs entirely on the event loop so the token generation and backend
        release are atomic with respect to other threads.

        Raises:
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        token = generate_thread_token(self._config.worker, self._token_nonce)
        await self.do_exit(token)

    async def do_exit(self, token: str) -> None:
        """Handle exit logic: release or re-acquire based on elapsed time."""
        if self._acquired_at is None:
            raise LockNotOwnedError(name=self._name)

        elapsed = monotonic() - self._acquired_at
        self._acquired_at = None
        self._token_nonce = generate_token_nonce()

        if elapsed >= self._config.min_lock_seconds:
            # Task took longer than min_lock_seconds, release immediately
            released = await self.do_release(token)
            if not released:
                raise LockNotOwnedError(name=self._name)
        else:
            # Re-acquire with remaining duration so the lock is held
            # until min_lock_seconds.
            remaining = self._config.min_lock_seconds - elapsed
            re_acquired = await self.do_reacquire(token, remaining)
            if not re_acquired:
                raise LockNotOwnedError(name=self._name)


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
        from_thread.run(self._task_lock.do_thread_enter)
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
        from_thread.run(self._task_lock.do_thread_exit)

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return from_thread.run(self._task_lock.locked)
