"""Task Lock.

A distributed lock for scheduled tasks with two time boundaries:
- min_lock_seconds: Prevents re-execution on other nodes after task completes.
- max_lock_seconds: Auto-expires the lock (deadlock protection).
"""

import asyncio
from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import Annotated, Self
from uuid import UUID

from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro._config import Reconfigurable, env_segment, resolve_config
from grelmicro.coordination._base import BaseLockConfig
from grelmicro.coordination._tokens import (
    generate_task_token,
    generate_thread_token,
    generate_token_nonce,
)
from grelmicro.coordination.abc import LockBackend, LockPrimitive, Seconds
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.errors import WouldBlockError

logger = getLogger("grelmicro.coordination")


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


class TaskLock(Reconfigurable[TaskLockConfig], LockPrimitive):
    """Task Lock.

    A distributed lock for scheduled tasks. Unlike a regular Lock,
    TaskLock does not release immediately on context manager exit. Instead, it keeps
    the lock held for at least `min_lock_seconds` seconds to prevent re-execution
    on other nodes.

    There is no background task that maintains the lock active during execution.
    The lock relies entirely on the TTL (`max_lock_seconds`) set at acquire time.

    This lock is designed to be used as the `sync` parameter of `IntervalTask`.

    Supports live reconfiguration via
    `reconfigure(new_config)`.
    A swap takes effect on the next call. An exit re-acquire uses
    the config the call entered with. The `worker` field cannot
    change. Changing it raises `ValueError`. See
    [Live reconfiguration](../architecture/reconfigure.md).
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
            LockBackend | str | None,
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
                before this duration has elapsed. When unset and env reads
                are enabled (see `env_load` and `GREL_ENV_LOAD`),
                resolves from the environment variable
                `GREL_TASKLOCK_{NAME_UPPER}_MIN_LOCK_SECONDS` if
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

                Default: 60. Acts as the TTL on acquire. When unset and env reads
                are enabled (see `env_load` and `GREL_ENV_LOAD`),
                resolves from the environment variable
                `GREL_TASKLOCK_{NAME_UPPER}_MAX_LOCK_SECONDS` if
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

                Default: `GREL_TASKLOCK_{NAME_UPPER}_`. Set this to
                a custom prefix when the application uses a different
                naming convention.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_ENV_LOAD`` flag. Pass True or False to
                override the flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the task lock."""
        resolved_env_prefix = (
            env_prefix or f"GREL_TASKLOCK_{env_segment(name)}_"
        )
        config = resolve_config(
            TaskLockConfig,
            explicit=None,
            kwargs={
                "worker": worker,
                "min_lock_seconds": min_lock_seconds,
                "max_lock_seconds": max_lock_seconds,
            },
            env_prefix=resolved_env_prefix,
            env_load=env_load,
        )
        self._setup(name, config, backend)
        self._track_reconfigure(resolved_env_prefix)

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
            LockBackend | str | None,
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
        backend: LockBackend | str | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._lock_name = f"{self._LOCK_PREFIX}:{name}"
        self._backend: LockBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )
        self._acquired_at: float | None = None
        self._token_nonce = generate_token_nonce()
        self._from_thread: ThreadTaskLockAdapter | None = None

    @property
    def name(self) -> str:
        """Return the task lock identity."""
        return self._name

    @property
    def backend(self) -> LockBackend:
        """Bound lock backend, resolved on each call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the active `Grelmicro` app is
        consulted via `Grelmicro.current()` on every access so that
        `micro.override(Coordination(...))` blocks take effect.
        """
        if self._backend is not None:
            return self._backend
        coordination = Grelmicro.current().get(
            "coordination", self._backend_name or "default"
        )
        return coordination.lock_backend

    async def __aenter__(self) -> Self:
        """Acquire the lock with duration=max_lock_seconds.

        Raises:
            WouldBlockError: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        config = self._config
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._name)

        token = generate_task_token(config.worker, self._token_nonce)
        if not await self.do_acquire(token, duration=config.max_lock_seconds):
            msg = f"Task lock not acquired: name={self._name}, token={token}"
            raise WouldBlockError(msg)

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
        config = self._config
        token = generate_task_token(config.worker, self._token_nonce)
        await self.do_exit(token, min_lock_seconds=config.min_lock_seconds)

    @property
    def from_thread(self) -> "ThreadTaskLockAdapter":
        """Return the task lock adapter for worker thread."""
        if self._from_thread is None:
            self._from_thread = ThreadTaskLockAdapter(task_lock=self)
        return self._from_thread

    async def refresh(self) -> None:
        """Renew the lease for another `max_lock_seconds` without releasing.

        Raises:
            LockNotOwnedError: If this task does not hold the lock or the lease was lost.
            LockAcquireError: If the backend call fails.
        """
        config = self._config
        if self._acquired_at is None:
            raise LockNotOwnedError(name=self._name)
        token = generate_task_token(config.worker, self._token_nonce)
        renewed = await self.do_reacquire(token, config.max_lock_seconds)
        if not renewed:
            raise LockNotOwnedError(name=self._name)

    async def locked(self) -> bool:
        """Check if the lock is acquired.

        Raises:
            LockLockedCheckError: If the lock cannot be checked due to an error on the backend.
        """
        backend = self.backend
        try:
            return await backend.locked(name=self._lock_name)
        except Exception as exc:
            raise LockLockedCheckError(name=self._name) from exc

    async def do_acquire(self, token: str, *, duration: Seconds) -> bool:
        """Acquire the lock.

        This method should not be called directly. Use the context manager instead.

        Args:
            token: The token to register on the backend.
            duration: The lease duration to request, in seconds. The
                caller captures this from
                `self._config.max_lock_seconds` at the start of the
                operation so a concurrent `reconfigure` cannot change
                the duration mid-acquire.

        Returns:
            bool: True if the lock was acquired, False if the lock was not acquired.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        backend = self.backend
        try:
            # TaskLock does not surface the fencing token. A non-None result
            # means the lock was acquired.
            fencing_token = await backend.acquire(
                name=self._lock_name,
                token=token,
                duration=duration,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._name) from exc
        acquired = fencing_token is not None
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
        backend = self.backend
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
        backend = self.backend
        try:
            # TaskLock does not surface the fencing token. A non-None result
            # means the lock was re-acquired.
            return (
                await backend.acquire(
                    name=self._lock_name,
                    token=token,
                    duration=duration,
                )
            ) is not None
        except Exception as exc:
            raise LockReleaseError(name=self._name) from exc

    async def do_thread_enter(self) -> None:
        """Acquire the lock from a worker thread.

        Runs entirely on the event loop so the reentrant check, token
        generation, and backend acquire are atomic with respect to other
        threads.

        Raises:
            WouldBlockError: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        config = self._config
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._name)

        token = generate_thread_token(config.worker, self._token_nonce)
        if not await self.do_acquire(token, duration=config.max_lock_seconds):
            msg = f"Task lock not acquired: name={self._name}, token={token}"
            raise WouldBlockError(msg)

    async def do_thread_exit(self) -> None:
        """Release or extend the lock from a worker thread.

        Runs entirely on the event loop so the token generation and backend
        release are atomic with respect to other threads.

        Raises:
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        config = self._config
        token = generate_thread_token(config.worker, self._token_nonce)
        await self.do_exit(token, min_lock_seconds=config.min_lock_seconds)

    async def _apply_reconfigure(self, new_config: TaskLockConfig) -> None:
        """Validate the immutable `worker` field before publishing `new_config`."""
        if new_config.worker != self._config.worker:
            msg = (
                f"reconfigure cannot change worker "
                f"({self._config.worker!r} -> {new_config.worker!r}). "
                f"Reuse the existing worker on the new config."
            )
            raise ValueError(msg)

    async def do_exit(self, token: str, *, min_lock_seconds: Seconds) -> None:
        """Handle exit logic: release or re-acquire based on elapsed time.

        Args:
            token: The token used to release or re-acquire the lock.
            min_lock_seconds: The minimum hold duration to enforce, in
                seconds. The caller captures this from
                `self._config.min_lock_seconds` at the start of the
                operation so the comparison and the
                remaining-duration calculation always agree.
        """
        if self._acquired_at is None:
            raise LockNotOwnedError(name=self._name)

        elapsed = monotonic() - self._acquired_at
        self._acquired_at = None
        self._token_nonce = generate_token_nonce()

        if elapsed >= min_lock_seconds:
            # Task took longer than min_lock_seconds, release immediately
            released = await self.do_release(token)
            if not released:
                raise LockNotOwnedError(name=self._name)
        else:
            # Re-acquire with remaining duration so the lock is held
            # until min_lock_seconds.
            remaining = min_lock_seconds - elapsed
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
            WouldBlockError: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
        """
        asyncio.run_coroutine_threadsafe(
            self._task_lock.do_thread_enter(),
            self._task_lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()
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
        asyncio.run_coroutine_threadsafe(
            self._task_lock.do_thread_exit(),
            self._task_lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return asyncio.run_coroutine_threadsafe(
            self._task_lock.locked(),
            self._task_lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()
