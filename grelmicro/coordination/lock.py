"""Lock."""

import asyncio
import re
from threading import Thread, current_thread
from types import TracebackType
from typing import Annotated, ClassVar, Self
from uuid import UUID
from weakref import WeakSet

from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._app import resolve_ambient
from grelmicro._async import raise_backend_not_open
from grelmicro._config import (
    Reconfigurable,
    env_prefixes,
    resolve_config,
)
from grelmicro.coordination._base import (
    BaseLock,
    BaseLockConfig,
    assert_worker_unchanged,
    jittered_interval,
)
from grelmicro.coordination._handle import LockHandle
from grelmicro.coordination._protocol import LockBackend, Seconds
from grelmicro.coordination._tokens import (
    generate_task_token,
    generate_thread_token,
)
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.errors import (
    LockTimeoutError,
    OutOfContextError,
    SettingsValidationError,
    WouldBlockError,
)

_MIN_RETRY_INTERVAL: float = 0.001
_NAME_MAX_LEN = 200
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]*$")


def validate_lock_name(name: str) -> None:
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
        raise SettingsValidationError(msg)


class LockConfig(BaseLockConfig):
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
    retry_jitter: Annotated[
        float,
        Doc(
            """
            Factor for randomized jitter applied to each retry sleep.

            Each sleep becomes retry_interval * uniform(1 - retry_jitter, 1 + retry_jitter).
            Set to 0 to disable jitter. Must be >= 0 and < 1.
            """,
        ),
    ] = 0.1

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.retry_interval < _MIN_RETRY_INTERVAL:
            msg = f"retry_interval must be >= {_MIN_RETRY_INTERVAL}"
            raise ValueError(msg)
        if not (0 <= self.retry_jitter < 1):
            msg = "retry_jitter must be >= 0 and < 1"
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

    _IMMUTABLE_RECONFIGURE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"worker"}
    )

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

                By default, a random 16-character hex token is generated.
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
                variable `GREL_LOCK_LEASE_DURATION` for the default
                instance (`GREL_LOCK_{NAME_UPPER}_LEASE_DURATION` for a
                named one) if present, otherwise falls back to the
                `LockConfig` default.
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
                `GREL_LOCK_RETRY_INTERVAL` for the default instance
                (`GREL_LOCK_{NAME_UPPER}_RETRY_INTERVAL` for a named
                one) if present, otherwise falls back to the
                `LockConfig` default.
                """,
            ),
        ] = None,
        retry_jitter: Annotated[
            float | None,
            Doc(
                """
                Factor for randomized jitter applied to each retry sleep.

                Default: 0.1. Each sleep becomes
                retry_interval * uniform(1 - retry_jitter, 1 + retry_jitter).
                Set to 0 to disable jitter. When unset and env reads are
                enabled (see ``env_load`` and ``GREL_ENV_LOAD``), resolves
                from the environment variable
                `GREL_LOCK_RETRY_JITTER` for the default instance
                (`GREL_LOCK_{NAME_UPPER}_RETRY_JITTER` for a named one)
                if present, otherwise falls back to the `LockConfig`
                default.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_LOCK_` for the default instance,
                `GREL_LOCK_{NAME_UPPER}_` for a named one. Set this to a
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

                Pass False when the values here are the whole truth.
                Env reads fill every field not passed, so a config
                half taken from somewhere else silently gets the rest
                from the environment.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the lock."""
        resolved_env_prefix, kind_prefix = env_prefixes(
            "LOCK", name, env_prefix
        )
        config = resolve_config(
            LockConfig,
            explicit=None,
            kwargs={
                "worker": worker,
                "lease_duration": lease_duration,
                "retry_interval": retry_interval,
                "retry_jitter": retry_jitter,
            },
            env_prefix=resolved_env_prefix,
            kind_env_prefix=kind_prefix,
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
        validate_lock_name(name)
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
        # WeakSet so a holder that exits without releasing does not pin
        # its object in memory and does not risk colliding with a future
        # holder that lands on the same id(). Threads need it as much as
        # tasks: a set of idents told the next thread it already held a
        # lock it never took.
        self._held_by_tasks: WeakSet[asyncio.Task[object]] = WeakSet()
        self._held_by_threads: WeakSet[Thread] = WeakSet()
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
        consulted on every access so that
        `micro.override(Coordination(...))` blocks take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope. Pass
                `backend=` (a `MemoryLockAdapter()` for a per-process
                lock), register a `Coordination` Component, or run the
                call inside `async with micro:` or after
                `micro.install(app)`.
        """
        if self._backend is not None:
            return self._backend
        try:
            coordination = resolve_ambient(
                ("coordination", self._backend_name or "default")
            )
        except LookupError:
            msg = (
                f"Lock({self._name!r}) resolved no backend. Pass backend= "
                f"(MemoryLockAdapter() for a per-process lock), register a "
                f"Coordination component, or run the call inside "
                f"`async with micro:` or after `micro.install(app)`."
            )
            raise OutOfContextError(msg) from None
        return coordination.lock_backend

    async def __aenter__(self) -> LockHandle:
        """Acquire the lock with the async context manager.

        Returns the `LockHandle` for this acquisition so the body can read
        `held.fencing_token`.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        return await self.acquire()

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
        return None

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

    async def acquire(
        self,
        *,
        timeout: Annotated[  # noqa: ASYNC109
            "Seconds | None",
            Doc(
                """
                Maximum number of seconds to wait for the lock.

                When None (the default), waits indefinitely. When set to
                a positive number, retries until the deadline then raises
                LockTimeoutError.
                """,
            ),
        ] = None,
    ) -> LockHandle:
        """Acquire the lock.

        Returns the `LockHandle` for this acquisition, carrying the ownership
        `token` and the strictly increasing `fencing_token`.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
            LockTimeoutError: If `timeout` is set and the lock was not acquired
                within that time. Subclasses builtin `TimeoutError`.

        """
        config = self._config
        task = self._running_task()
        if task in self._held_by_tasks:
            raise LockReentrantError(name=self._name)
        token = generate_task_token(config.worker)
        duration = config.lease_duration
        # Stamped whether or not a timeout is set, so the deadline is
        # `started + timeout` and the guard narrows `timeout` where it
        # reports the wait that elapsed.
        started = asyncio.get_running_loop().time()
        jitter = config.retry_jitter
        fencing_token = await self.do_acquire(token=token, duration=duration)
        while fencing_token is None:
            if (
                timeout is not None
                and asyncio.get_running_loop().time() >= started + timeout
            ):
                raise LockTimeoutError(name=self._name, timeout=timeout)
            interval = jittered_interval(config.retry_interval, jitter)
            await asyncio.sleep(interval)
            fencing_token = await self.do_acquire(
                token=token, duration=duration
            )
        self._held_by_tasks.add(task)
        return LockHandle(
            name=self._name, token=token, fencing_token=fencing_token
        )

    async def extend(self) -> LockHandle:
        """Renew the lease for another `lease_duration` without releasing.

        The fencing token is unchanged when the lease is still held. If the
        lease has expired or was released by another path, the backend returns
        None and this method raises `LockNotOwnedError`.

        Returns:
            LockHandle: The handle for this lock with the same fencing token.

        Raises:
            LockNotOwnedError: If this task does not hold the lock or the lease was lost.
            LockAcquireError: If the backend call fails.

        """
        config = self._config
        task = self._running_task()
        if task not in self._held_by_tasks:
            raise LockNotOwnedError(name=self._name)
        token = generate_task_token(config.worker)
        fencing_token = await self.do_acquire(
            token=token, duration=config.lease_duration
        )
        if fencing_token is None:
            raise LockNotOwnedError(name=self._name)
        return LockHandle(
            name=self._name, token=token, fencing_token=fencing_token
        )

    async def acquire_nowait(self) -> LockHandle:
        """Acquire the lock, without blocking.

        Returns the `LockHandle` for this acquisition, carrying the ownership
        `token` and the strictly increasing `fencing_token`.

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
        fencing_token = await self.do_acquire(
            token=token, duration=config.lease_duration
        )
        if fencing_token is None:
            msg = f"Lock not acquired: name={self._name}, token={token}"
            raise WouldBlockError(msg)
        self._held_by_tasks.add(task)
        return LockHandle(
            name=self._name, token=token, fencing_token=fencing_token
        )

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

    async def do_acquire(self, token: str, *, duration: Seconds) -> int | None:
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
            int | None: The fencing token if the lock was acquired, None if
                the lock was not acquired.

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
        assert_worker_unchanged(self._config, new_config)

    async def do_thread_acquire(
        self,
        owner: Thread,
        *,
        timeout: "Seconds | None" = None,  # noqa: ASYNC109
    ) -> LockHandle:
        """Acquire the lock from a worker thread (blocking).

        Runs on the event loop so the reentrant check and backend acquire
        are atomic with respect to other threads.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
            LockTimeoutError: If `timeout` is set and the lock was not acquired
                within that time. Subclasses builtin `TimeoutError`.
        """
        config = self._config
        if owner in self._held_by_threads:
            raise LockReentrantError(name=self._name)
        token = generate_thread_token(config.worker, owner=owner)
        duration = config.lease_duration
        # Stamped whether or not a timeout is set, so the deadline is
        # `started + timeout` and the guard narrows `timeout` where it
        # reports the wait that elapsed.
        started = asyncio.get_running_loop().time()
        jitter = config.retry_jitter
        fencing_token = await self.do_acquire(token=token, duration=duration)
        while fencing_token is None:
            if (
                timeout is not None
                and asyncio.get_running_loop().time() >= started + timeout
            ):
                raise LockTimeoutError(name=self._name, timeout=timeout)
            interval = jittered_interval(config.retry_interval, jitter)
            await asyncio.sleep(interval)
            fencing_token = await self.do_acquire(
                token=token, duration=duration
            )
        self._held_by_threads.add(owner)
        return LockHandle(
            name=self._name, token=token, fencing_token=fencing_token
        )

    async def do_thread_extend(self, owner: Thread) -> LockHandle:
        """Renew the lease from a worker thread without releasing.

        Runs on the event loop so the ownership check and backend acquire
        are atomic with respect to other threads.

        Raises:
            LockNotOwnedError: If this thread does not hold the lock or the lease was lost.
            LockAcquireError: If the backend call fails.
        """
        config = self._config
        if owner not in self._held_by_threads:
            raise LockNotOwnedError(name=self._name)
        token = generate_thread_token(config.worker, owner=owner)
        fencing_token = await self.do_acquire(
            token=token, duration=config.lease_duration
        )
        if fencing_token is None:
            raise LockNotOwnedError(name=self._name)
        return LockHandle(
            name=self._name, token=token, fencing_token=fencing_token
        )

    async def do_thread_acquire_nowait(self, owner: Thread) -> LockHandle:
        """Acquire the lock from a worker thread (non-blocking).

        Runs on the event loop so the reentrant check and backend acquire
        are atomic with respect to other threads.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            WouldBlockError: If the lock cannot be acquired without blocking.
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        config = self._config
        if owner in self._held_by_threads:
            raise LockReentrantError(name=self._name)
        token = generate_thread_token(config.worker, owner=owner)
        fencing_token = await self.do_acquire(
            token=token, duration=config.lease_duration
        )
        if fencing_token is None:
            msg = f"Lock not acquired: name={self._name}, token={token}"
            raise WouldBlockError(msg)
        self._held_by_threads.add(owner)
        return LockHandle(
            name=self._name, token=token, fencing_token=fencing_token
        )

    async def do_thread_release(self, owner: Thread) -> None:
        """Release the lock from a worker thread.

        Runs on the event loop so the backend release is atomic with respect
        to other threads.

        Raises:
            LockNotOwnedError: If the lock is not owned by the current token.
            LockReleaseError: If the lock cannot be released due to an error on the backend.
        """
        token = generate_thread_token(self._config.worker, owner=owner)
        released = await self.do_release(token)
        self._held_by_threads.discard(owner)
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

    @property
    def _backend_loop(self) -> asyncio.AbstractEventLoop:
        """Return the event loop the backend captured on ``__aenter__``."""
        loop = self._lock.backend._loop  # noqa: SLF001
        if loop is None:
            raise_backend_not_open(f"Lock {self._lock.name!r}")
        return loop

    def __enter__(self) -> LockHandle:
        """Acquire the lock with the context manager.

        Returns the `LockHandle` for this acquisition so the body can read
        `held.fencing_token`.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.
        """
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the lock with the context manager."""
        self.release()

    def acquire(self, *, timeout: "Seconds | None" = None) -> LockHandle:
        """Acquire the lock.

        Returns the `LockHandle` for this acquisition, carrying the ownership
        `token` and the strictly increasing `fencing_token`.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.
            LockTimeoutError: If `timeout` is set and the lock was not acquired
                within that time. Subclasses builtin `TimeoutError`.
        """
        return asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_acquire(current_thread(), timeout=timeout),
            self._backend_loop,
        ).result()

    def extend(self) -> LockHandle:
        """Renew the lease without releasing.

        Returns the `LockHandle` with the same fencing token.

        Raises:
            LockNotOwnedError: If this thread does not hold the lock or the lease was lost.
            LockAcquireError: Cannot extend the lock due to backend error.
        """
        return asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_extend(current_thread()),
            self._backend_loop,
        ).result()

    def acquire_nowait(self) -> LockHandle:
        """Acquire the lock, without blocking.

        Returns the `LockHandle` for this acquisition, carrying the ownership
        `token` and the strictly increasing `fencing_token`.

        Raises:
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            LockAcquireError: Cannot acquire the lock due to backend error.
            WouldBlockError: If the lock cannot be acquired without blocking.
        """
        return asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_acquire_nowait(current_thread()),
            self._backend_loop,
        ).result()

    def release(self) -> None:
        """Release the lock.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
            LockNotOwnedError: If the lock is not currently held.
        """
        asyncio.run_coroutine_threadsafe(
            self._lock.do_thread_release(current_thread()),
            self._backend_loop,
        ).result()

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        return asyncio.run_coroutine_threadsafe(
            self._lock.locked(),
            self._backend_loop,
        ).result()

    def owned(self) -> bool:
        """Return True if the lock is currently held by the current worker thread."""
        return asyncio.run_coroutine_threadsafe(
            self._lock.do_owned(
                generate_thread_token(
                    self._lock._config.worker,  # noqa: SLF001
                    owner=current_thread(),
                ),
            ),
            self._backend_loop,
        ).result()
