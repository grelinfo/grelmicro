"""Lock."""

import asyncio
import re
from threading import get_ident
from types import TracebackType
from typing import Annotated, Self
from uuid import UUID
from weakref import WeakSet

from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro._config import Reconfigurable, env_segment, resolve_config
from grelmicro.coordination._base import BaseLock, BaseLockConfig
from grelmicro.coordination._tokens import generate_task_token
from grelmicro.coordination.abc import LockBackend, Seconds
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.errors import WouldBlockError

_MIN_RETRY_INTERVAL: float = 0.001
_NAME_MAX_LEN = 200
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]*$")


def _validate_lock_name(name: str) -> None:
    """Reject lock names that would land as ugly or ambiguous backend keys.

    The pattern accepts letters, digits, and the separators ``._:/-`` after
    a leading alphanumeric, up to 200 characters. This blocks whitespace,
    control characters, and shell metacharacters while staying broad
    enough for namespaced names like ``users:42`` or ``payments/eu``.
    """
    if not name or len(name) > _NAME_MAX_LEN or not _NAME_PATTERN.match(name):
        msg = (
            f"Invalid lock name {name!r}: must match "
            f"^[A-Za-z0-9][A-Za-z0-9._:/-]*$ and be at most "
            f"{_NAME_MAX_LEN} chars. "
            f"Valid examples: 'cart', 'users:42', 'payments/eu'."
        )
        raise ValueError(msg)


class LockConfig(BaseLockConfig, frozen=True, extra="forbid"):  # ty: ignore[invalid-frozen-dataclass-subclass]
    """Lock Config."""

    lease_duration: Annotated[
        Seconds,
        Doc(
            """
            The lease duration in seconds for the lock.
            """,
        ),
    ] = 60
    retry_interval: Annotated[
        Seconds,
        Doc(
            """
            The interval in seconds between attempts to acquire the lock.

            Must be >= 0.001 to prevent flooding the lock backend.
            """,
        ),
    ] = 0.1

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.retry_interval < _MIN_RETRY_INTERVAL:
            msg = f"retry_interval must be >= {_MIN_RETRY_INTERVAL}"
            raise ValueError(msg)
        return self


class Lock(Reconfigurable[LockConfig], BaseLock):
    """Lock.

    This lock is a distributed lock that is used to acquire a resource across multiple workers. The
    lock is acquired asynchronously and can be extended multiple times manually. The lock is
    automatically released after a duration if not extended.

    Supports live reconfiguration via
    `reconfigure(new_config)`.
    A swap takes effect on the next call. In-flight calls keep the
    config they started with. The `worker` field cannot change.
    Changing it raises `ValueError`. See
    [Live reconfiguration](../architecture/reconfigure.md).
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
            LockBackend | str | None,
            Doc("""
                The distributed lock backend used to acquire and release the lock.

                Accepts a backend instance, the name of a registered backend
                (e.g. `"analytics"`), or `None` to use the registered
                `"default"` backend.
                """),
        ] = None,
        worker: Annotated[
            str | UUID | None,
            Doc(
                """
                The worker identity.

                By default, a UUIDv1 is generated.
                """,
            ),
        ] = None,
        lease_duration: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds for the lock to be held by default.

                Default: 60. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the environment
                variable `GREL_LOCK_{NAME_UPPER}_LEASE_DURATION` if
                present, otherwise falls back to the `LockConfig`
                default.
                """,
            ),
        ] = None,
        retry_interval: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds between attempts to acquire the lock.

                Default: 0.1. Must be >= 0.001 to prevent flooding
                the lock backend. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the
                environment variable
                `GREL_LOCK_{NAME_UPPER}_RETRY_INTERVAL` if present,
                otherwise falls back to the `LockConfig` default.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_LOCK_{NAME_UPPER}_`. Set this to a
                custom prefix when the application uses a different
                naming convention, for example `MYAPP_LOCK_CART_`.
                """,
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
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the lock."""
        config = resolve_config(
            LockConfig,
            explicit=None,
            kwargs={
                "worker": worker,
                "lease_duration": lease_duration,
                "retry_interval": retry_interval,
            },
            env_prefix=env_prefix or f"GREL_LOCK_{env_segment(name)}_",
            env_load=env_load,
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
                """,
            ),
        ],
        config: Annotated[
            LockConfig,
            Doc(
                """
                The pre-built lock configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a `pydantic-settings` aggregator). The environment
                path is bypassed and the config is used as-is.
                """,
            ),
        ],
        *,
        backend: Annotated[
            LockBackend | str | None,
            Doc("""
                The distributed lock backend used to acquire and release the lock.

                Accepts a backend instance, the name of a registered backend
                (e.g. `"analytics"`), or `None` to use the registered
                `"default"` backend.
                """),
        ] = None,
    ) -> Self:
        """Construct a `Lock` from a name and a pre-built `LockConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: LockConfig,
        backend: LockBackend | str | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        _validate_lock_name(name)
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
        # WeakSet so a task that exits without releasing does not
        # pin its Task object in memory and does not risk colliding
        # with a future task that ends up at the same id().
        self._held_by_tasks: WeakSet[asyncio.Task[object]] = WeakSet()
        self._held_by_threads: set[int] = set()
        self._from_thread: ThreadLockAdapter | None = None

    @property
    def name(self) -> str:
        """Return the lock identity."""
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
    def from_thread(self) -> "ThreadLockAdapter":
        """Return the lock adapter for a worker thread."""
        if self._from_thread is None:
            self._from_thread = ThreadLockAdapter(lock=self)
        return self._from_thread

    def _running_task(self) -> asyncio.Task[object]:
        """Return the running task."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover
            msg = "Lock async APIs must be called from a running asyncio task"
            raise RuntimeError(msg)
        return task

    async def acquire(self) -> None:
        """Acquire the lock.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.

        """
        config = self._config
        task = self._running_task()
        if task in self._held_by_tasks:
            raise LockReentrantError(name=self._name)
        token = generate_task_token(config.worker)
        duration = config.lease_duration
        while not await self.do_acquire(token=token, duration=duration):  # noqa: ASYNC110 // Polling is intentional
            await asyncio.sleep(config.retry_interval)
        self._held_by_tasks.add(task)

    async def acquire_nowait(self) -> None:
        """Acquire the lock, without blocking.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            WouldBlockError: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        config = self._config
        task = self._running_task()
        if task in self._held_by_tasks:
            raise LockReentrantError(name=self._name)
        token = generate_task_token(config.worker)
        if not await self.do_acquire(
            token=token, duration=config.lease_duration
        ):
            msg = f"Lock not acquired: name={self._name}, token={token}"
            raise WouldBlockError(msg)
        self._held_by_tasks.add(task)

    async def release(self) -> None:
        """Release the lock.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.

        """
        token = generate_task_token(self._config.worker)
        # Local ownership is cleared only after the backend has
        # responded. A backend error keeps the marker so the caller
        # can retry release. A "not owned" answer still clears it
        # because the distributed truth is authoritative.
        released = await self.do_release(token)
        self._held_by_tasks.discard(self._running_task())
        if not released:
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

    async def owned(self) -> bool:
        """Check if the lock is owned by the current token.

        Raises:
            LockBackendError: If the lock cannot be checked due to an error on the backend.
        """
        return await self.do_owned(generate_task_token(self._config.worker))

    async def do_acquire(self, token: str, *, duration: Seconds) -> bool:
        """Acquire the lock.

        This method should not be called directly. Use `acquire` instead.

        Args:
            token: The token to register on the backend.
            duration: The lease duration to request, in seconds. The
                caller captures this from `self._config.lease_duration`
                at the start of the operation so the request is
                consistent across retries even when `reconfigure`
                runs concurrently.

        Returns:
            bool: True if the lock was acquired, False if the lock was not acquired.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        backend = self.backend
        try:
            return await backend.acquire(
                name=self._lock_name,
                token=token,
                duration=duration,
            )
        except Exception as exc:
            raise LockAcquireError(name=self._name) from exc

    async def do_release(self, token: str) -> bool:
        """Release the lock.

        This method should not be called directly. Use `release` instead.

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

    async def do_owned(self, token: str) -> bool:
        """Check if the lock is owned by the current token.

        This method should not be called directly. Use `owned` instead.

        Returns:
            bool: True if the lock is owned by the current token, False otherwise.

        Raises:
            LockOwnedCheckError: Cannot check if the lock is owned due to backend error.
        """
        backend = self.backend
        try:
            return await backend.owned(name=self._lock_name, token=token)
        except Exception as exc:
            raise LockOwnedCheckError(name=self._name) from exc

    async def _apply_reconfigure(self, new_config: LockConfig) -> None:
        """Validate the immutable `worker` field before publishing `new_config`."""
        if new_config.worker != self._config.worker:
            msg = (
                f"reconfigure cannot change worker "
                f"({self._config.worker!r} -> {new_config.worker!r}). "
                f"Reuse the existing worker on the new config."
            )
            raise ValueError(msg)

    def _thread_token(self, thread_id: int, worker: str | UUID) -> str:
        """Build a thread token from a worker identity and the given thread ID."""
        return f"{worker}:thread:{thread_id}"

    async def do_thread_acquire(self, thread_id: int) -> None:
        """Acquire the lock from a worker thread (blocking).

        Runs on the event loop so the reentrant check and backend acquire
        are atomic with respect to other threads.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        config = self._config
        if thread_id in self._held_by_threads:
            raise LockReentrantError(name=self._name)
        token = self._thread_token(thread_id, config.worker)
        duration = config.lease_duration
        while not await self.do_acquire(token=token, duration=duration):  # noqa: ASYNC110 // Polling is intentional
            await asyncio.sleep(config.retry_interval)
        self._held_by_threads.add(thread_id)

    async def do_thread_acquire_nowait(self, thread_id: int) -> None:
        """Acquire the lock from a worker thread (non-blocking).

        Runs on the event loop so the reentrant check and backend acquire
        are atomic with respect to other threads.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            WouldBlockError: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        config = self._config
        if thread_id in self._held_by_threads:
            raise LockReentrantError(name=self._name)
        token = self._thread_token(thread_id, config.worker)
        if not await self.do_acquire(
            token=token, duration=config.lease_duration
        ):
            msg = f"Lock not acquired: name={self._name}, token={token}"
            raise WouldBlockError(msg)
        self._held_by_threads.add(thread_id)

    async def do_thread_release(self, thread_id: int) -> None:
        """Release the lock from a worker thread.

        Runs on the event loop so the backend release is atomic with respect
        to other threads.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.
        """
        token = self._thread_token(thread_id, self._config.worker)
        released = await self.do_release(token)
        self._held_by_threads.discard(thread_id)
        if not released:
            raise LockNotOwnedError(name=self._name)


class ThreadLockAdapter:
    """Lock adapter for a worker thread spawned from an asyncio event loop.

    Schedules the lock's coroutine methods back onto the event loop
    captured at construction (or first async op) using
    ``asyncio.run_coroutine_threadsafe``.
    """

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
        asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_acquire(get_ident()),
            self._lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()

    def acquire_nowait(self) -> None:
        """Acquire the lock, without blocking.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.
            WouldBlockError: If the lock cannot be acquired without blocking.
        """
        asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_acquire_nowait(get_ident()),
            self._lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()

    def release(self) -> None:
        """Release the lock.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
            LockNotOwnedError: If the lock is not currently held.
        """
        asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_release(get_ident()),
            self._lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return asyncio.run_coroutine_threadsafe(
            self._lock.locked(),
            self._lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()

    def owned(self) -> bool:
        """Return True if the lock is currently held by the current worker thread."""
        return asyncio.run_coroutine_threadsafe(
            self._lock.do_owned(
                self._lock._thread_token(  # noqa: SLF001
                    get_ident(),
                    self._lock._config.worker,  # noqa: SLF001
                ),
            ),
            self._lock.backend._loop,  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        ).result()
