"""Read-Write Lock."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING, Annotated, ClassVar, Self
from weakref import WeakKeyDictionary

from typing_extensions import Doc

from grelmicro._app import resolve_ambient
from grelmicro._async import (
    on_backend_loop,
    raise_backend_not_open,
    raise_event_loop_deadlock,
)
from grelmicro._config import (
    Reconfigurable,
    env_prefixes,
    resolve_config,
)
from grelmicro.coordination._base import (
    assert_worker_unchanged,
    jittered_interval,
)
from grelmicro.coordination._guards import ReadGuard, WriteGuard
from grelmicro.coordination._tokens import (
    HolderIdentity,
    current_thread_identity,
    generate_task_token,
    generate_thread_token,
)
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
    LockUpgradeError,
)
from grelmicro.coordination.lock import LockConfig, validate_lock_name
from grelmicro.errors import (
    LockTimeoutError,
    OutOfContextError,
    WouldBlockError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType
    from uuid import UUID

    from grelmicro.coordination._protocol import (
        ReadWriteLockBackend,
        ReadWriteLockState,
        Seconds,
        WriteGrant,
    )


class ReadWriteLockConfig(LockConfig):
    """Read-Write Lock Config.

    Same fields as `LockConfig`. `lease_duration` covers a reader lease, a
    writer lease, and the intent a waiting writer records.
    """


class ReadWriteLock(Reconfigurable[ReadWriteLockConfig]):
    """Read-Write Lock.

    A distributed lock that lets many readers hold a resource at once and
    keeps writers alone. `read` and `write` are two views of the same lock,
    each a full primitive with `acquire`, `acquire_nowait`, `extend`,
    `release`, and a `from_thread` adapter.

    The lock is writer-preferring: a writer refused because readers hold the
    lock records an intent, and readers arriving after it wait. Readers
    already inside keep their lease and can renew it until they finish.

    Supports live reconfiguration via `reconfigure(new_config)`. A swap takes
    effect on the next call. In-flight calls keep the config they started
    with. The `worker` field cannot change. Changing it raises `ValueError`.
    See [Live reconfiguration](../architecture/reconfigure.md).
    """

    _LOCK_PREFIX = "rwlock"

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

                It will be used as the lock name so make sure it is unique on
                the read-write lock backend.
                """,
            ),
        ],
        *,
        backend: Annotated[
            ReadWriteLockBackend | str | None,
            Doc("""
                The read-write lock backend used to acquire and release the
                lock.

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
                The duration in seconds a lease is held by default.

                Default: 60. Covers a reader lease, a writer lease, and a
                waiting writer's intent. When unset and env reads are enabled
                (see ``env_load`` and ``GREL_ENV_LOAD``), resolves from
                `GREL_READWRITELOCK_LEASE_DURATION` for the default instance
                (`GREL_READWRITELOCK_{NAME_UPPER}_LEASE_DURATION` for a named
                one) if present, otherwise falls back to the
                `ReadWriteLockConfig` default.
                """,
            ),
        ] = None,
        retry_interval: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds between attempts to acquire the lock.

                Default: 0.1. Must be >= 0.001 to prevent flooding the
                backend. Resolves from `GREL_READWRITELOCK_RETRY_INTERVAL`
                when unset and env reads are enabled.
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
                Set to 0 to disable jitter. Resolves from
                `GREL_READWRITELOCK_RETRY_JITTER` when unset and env reads are
                enabled.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_READWRITELOCK_` for the default instance,
                `GREL_READWRITELOCK_{NAME_UPPER}_` for a named one.
                """,
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_ENV_LOAD`` flag. Pass True or False to override the
                flag for this construction.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the read-write lock."""
        resolved_env_prefix, kind_prefix = env_prefixes(
            "READWRITELOCK", name, env_prefix
        )
        config = resolve_config(
            ReadWriteLockConfig,
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
            Doc("The name of the resource to lock."),
        ],
        config: Annotated[
            ReadWriteLockConfig,
            Doc(
                """
                The pre-built configuration.

                Use this path when the configuration is assembled at startup
                from a settings tree. The environment path is bypassed and the
                config is used as-is.
                """,
            ),
        ],
        *,
        backend: Annotated[
            ReadWriteLockBackend | str | None,
            Doc("The read-write lock backend, a registered name, or `None`."),
        ] = None,
    ) -> Self:
        """Construct a `ReadWriteLock` from a name and a pre-built config."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: ReadWriteLockConfig,
        backend: ReadWriteLockBackend | str | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        validate_lock_name(name)
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._lock_name = f"{self._LOCK_PREFIX}:{name}"
        self._backend: ReadWriteLockBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )
        self.read = ReadMode(self)
        self.write = WriteMode(self)

    @property
    def name(self) -> str:
        """Return the lock identity."""
        return self._name

    @property
    def backend(self) -> ReadWriteLockBackend:
        """Bound read-write lock backend, resolved on each call.

        When a backend instance was passed at construction it is always
        returned. Otherwise the active `Grelmicro` app is consulted on
        every access so that
        `micro.override(Coordination(...))` blocks take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope. Pass
                `backend=` (a `MemoryReadWriteLockAdapter()` for a per-process
                lock), register a `Coordination` Component, or run the call
                inside `async with micro:` or after `micro.install(app)`.
        """
        if self._backend is not None:
            return self._backend
        try:
            coordination = resolve_ambient(
                ("coordination", self._backend_name or "default")
            )
        except LookupError:
            msg = (
                f"ReadWriteLock({self._name!r}) resolved no backend. Pass "
                f"backend= (MemoryReadWriteLockAdapter() for a per-process "
                f"lock), register a Coordination component, or run the call "
                f"inside `async with micro:` or after `micro.install(app)`."
            )
            raise OutOfContextError(msg) from None
        return coordination.rwlock_backend

    async def state(self) -> ReadWriteLockState:
        """Return a point-in-time view of the lock.

        Raises:
            LockOwnedCheckError: The backend call failed.
        """
        backend = self.backend
        try:
            return await backend.state(name=self._lock_name)
        except Exception as exc:
            raise LockOwnedCheckError(name=self._name) from exc

    async def _apply_reconfigure(self, new_config: ReadWriteLockConfig) -> None:
        """Validate the immutable `worker` field before publishing."""
        assert_worker_unchanged(self._config, new_config)


class _Mode:
    """State and retry loop shared by the read and write views."""

    __slots__ = ("_lock", "_thread_adapter")

    def __init__(self, lock: ReadWriteLock) -> None:
        """Initialize the mode."""
        self._lock = lock

    @property
    def name(self) -> str:
        """Return the lock identity."""
        return self._lock.name

    @property
    def backend(self) -> ReadWriteLockBackend:
        """Return the bound backend."""
        return self._lock.backend

    def _running_task(self) -> asyncio.Task[object]:
        """Return the running task."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover
            msg = (
                "ReadWriteLock async APIs must be called from a running"
                " asyncio task"
            )
            raise RuntimeError(msg)
        return task

    async def _retry_until[T](
        self,
        attempt: Callable[[], Awaitable[T | None]],
        *,
        timeout: float | None,  # noqa: ASYNC109
    ) -> T:
        """Call `attempt` until it grants, or until the deadline passes.

        Raises:
            TimeoutError: `timeout` elapsed before the lock was granted.
        """
        config = self._lock._config  # noqa: SLF001
        # Stamped whether or not a timeout is set, so the deadline is
        # `started + timeout` and the guard narrows `timeout` where it
        # reports the wait that elapsed.
        started = asyncio.get_running_loop().time()
        granted = await attempt()
        while granted is None:
            if (
                timeout is not None
                and asyncio.get_running_loop().time() >= started + timeout
            ):
                raise LockTimeoutError(name=self.name, timeout=timeout)
            await asyncio.sleep(
                jittered_interval(config.retry_interval, config.retry_jitter)
            )
            granted = await attempt()
        return granted


class ReadMode(_Mode):
    """The read view of a `ReadWriteLock`.

    Many readers hold it at once. A reader waits while a writer holds the
    lock or waits for it.
    """

    __slots__ = ("_task_guards", "_thread_guards")

    def __init__(self, lock: ReadWriteLock) -> None:
        """Initialize the read view."""
        super().__init__(lock)
        self._task_guards: WeakKeyDictionary[
            asyncio.Task[object], ReadGuard
        ] = WeakKeyDictionary()
        self._thread_guards: WeakKeyDictionary[HolderIdentity, ReadGuard] = (
            WeakKeyDictionary()
        )
        self._thread_adapter: ThreadReadAdapter | None = None

    async def __aenter__(self) -> ReadGuard:
        """Acquire the read lock.

        Raises:
            LockReentrantError: This task already holds this lock.
            LockAcquireError: The backend call failed.
        """
        return await self.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the read lock.

        Raises:
            LockNotOwnedError: This task no longer holds the lock.
            LockReleaseError: The backend call failed.
        """
        await self.release()

    @property
    def from_thread(self) -> ThreadReadAdapter:
        """Return the read adapter for a worker thread."""
        if self._thread_adapter is None:
            self._thread_adapter = ThreadReadAdapter(mode=self)
        return self._thread_adapter

    async def acquire(
        self,
        *,
        timeout: Annotated[  # noqa: ASYNC109
            Seconds | None,
            Doc(
                """
                Maximum number of seconds to wait for the lock.

                When None (the default), waits indefinitely. When set,
                retries until the deadline then raises LockTimeoutError.
                """,
            ),
        ] = None,
    ) -> ReadGuard:
        """Acquire the read lock, waiting for any writer to finish.

        Raises:
            LockReentrantError: This task already holds this lock.
            LockAcquireError: The backend call failed.
            TimeoutError: `timeout` elapsed before the lock was granted.
        """
        task = self._running_task()
        self._assert_free(task)
        token = generate_task_token(self._lock._config.worker)  # noqa: SLF001
        duration = self._lock._config.lease_duration  # noqa: SLF001
        generation = await self._retry_until(
            lambda: self.do_acquire(token, duration=duration),
            timeout=timeout,
        )
        guard = self._new_guard(token, generation, duration)
        self._task_guards[task] = guard
        return guard

    async def acquire_nowait(self) -> ReadGuard:
        """Acquire the read lock, without waiting.

        Raises:
            LockReentrantError: This task already holds this lock.
            WouldBlockError: A writer holds the lock or waits for it.
            LockAcquireError: The backend call failed.
        """
        task = self._running_task()
        self._assert_free(task)
        token = generate_task_token(self._lock._config.worker)  # noqa: SLF001
        duration = self._lock._config.lease_duration  # noqa: SLF001
        generation = await self.do_acquire(token, duration=duration)
        if generation is None:
            msg = f"Read lock not acquired: name={self.name}, token={token}"
            raise WouldBlockError(msg)
        guard = self._new_guard(token, generation, duration)
        self._task_guards[task] = guard
        return guard

    async def extend(self) -> None:
        """Renew this task's read lease.

        Raises:
            LockNotOwnedError: This task holds no live read lease.
            LockAcquireError: The backend call failed.
        """
        await self.do_extend(self._guard_or_raise(self._running_task()))

    async def release(self) -> None:
        """Release this task's read lease.

        Raises:
            LockNotOwnedError: This task holds no live read lease.
            LockReleaseError: The backend call failed.
        """
        task = self._running_task()
        guard = self._guard_or_raise(task)
        released = await self._drop_lease(guard)
        del self._task_guards[task]
        if not released:
            raise LockNotOwnedError(name=self.name)

    async def owned(self) -> bool:
        """Return whether this task holds a live read lease.

        Raises:
            LockOwnedCheckError: The backend call failed.
        """
        guard = self._task_guards.get(self._running_task())
        if guard is None:
            return False
        return await self._owned_on_backend(guard.token)

    def _assert_free(self, task: asyncio.Task[object]) -> None:
        """Reject a nested acquire from a task that already holds the lock.

        Raises:
            LockReentrantError: The task holds the read or the write lock.
        """
        if task in self._task_guards or task in self._lock.write._task_guards:  # noqa: SLF001
            raise LockReentrantError(name=self.name)

    def _guard_or_raise(self, holder: asyncio.Task[object]) -> ReadGuard:
        """Return the guard held by `holder`.

        Raises:
            LockNotOwnedError: The holder holds no read lease.
        """
        guard = self._task_guards.get(holder)
        if guard is None:
            raise LockNotOwnedError(name=self.name)
        return guard

    def _new_guard(
        self, token: str, generation: int, duration: float
    ) -> ReadGuard:
        """Build a guard for a granted read lease."""
        return ReadGuard(
            owner=self,
            name=self.name,
            token=token,
            generation=generation,
            expires_at=monotonic() + duration,
        )

    async def _drop_lease(self, guard: ReadGuard) -> bool:
        """Drop a read lease on the backend and spend its guard.

        Returns whether the backend still held the lease.

        Raises:
            LockReleaseError: The backend call failed.
        """
        backend = self.backend
        try:
            released = await backend.release_read(
                name=self._lock._lock_name,  # noqa: SLF001
                token=guard.token,
            )
        except Exception as exc:
            raise LockReleaseError(name=self.name) from exc
        guard._invalidate()  # noqa: SLF001
        return released

    async def _owned_on_backend(self, token: str) -> bool:
        """Ask the backend whether `token` holds a live read lease.

        Raises:
            LockOwnedCheckError: The backend call failed.
        """
        backend = self.backend
        try:
            return await backend.owned_read(
                name=self._lock._lock_name,  # noqa: SLF001
                token=token,
            )
        except Exception as exc:
            raise LockOwnedCheckError(name=self.name) from exc

    async def do_acquire(self, token: str, *, duration: float) -> int | None:
        """Ask the backend for a read lease.

        Raises:
            LockAcquireError: The backend call failed.
        """
        backend = self.backend
        try:
            return await backend.acquire_read(
                name=self._lock._lock_name,  # noqa: SLF001
                token=token,
                duration=duration,
            )
        except Exception as exc:
            raise LockAcquireError(name=self.name) from exc

    async def do_extend(self, guard: ReadGuard) -> None:
        """Renew the lease behind `guard`.

        Raises:
            LockNotOwnedError: The lease was lost.
            LockAcquireError: The backend call failed.
        """
        duration = self._lock._config.lease_duration  # noqa: SLF001
        generation = await self.do_acquire(guard.token, duration=duration)
        if generation is None:
            guard._invalidate()  # noqa: SLF001
            raise LockNotOwnedError(name=self.name)
        guard._renewed(monotonic() + duration)  # noqa: SLF001

    def _adopt(self, guard: ReadGuard, holder: asyncio.Task[object]) -> None:
        """Register a guard produced by a downgrade."""
        self._task_guards[holder] = guard

    async def do_thread_acquire(
        self,
        owner: HolderIdentity,
        *,
        timeout: Seconds | None = None,  # noqa: ASYNC109
    ) -> ReadGuard:
        """Acquire the read lock for a worker thread.

        Raises:
            LockReentrantError: This thread already holds this lock.
            LockAcquireError: The backend call failed.
            TimeoutError: `timeout` elapsed before the lock was granted.
        """
        if (
            owner in self._thread_guards
            or owner in self._lock.write._thread_guards  # noqa: SLF001
        ):
            raise LockReentrantError(name=self.name)
        config = self._lock._config  # noqa: SLF001
        token = generate_thread_token(config.worker, identity=owner)
        duration = config.lease_duration
        generation = await self._retry_until(
            lambda: self.do_acquire(token, duration=duration),
            timeout=timeout,
        )
        guard = self._new_guard(token, generation, duration)
        self._thread_guards[owner] = guard
        return guard

    async def do_thread_release(self, owner: HolderIdentity) -> None:
        """Release the read lease held by a worker thread.

        Raises:
            LockNotOwnedError: This thread holds no live read lease.
            LockReleaseError: The backend call failed.
        """
        guard = self._thread_guards.get(owner)
        if guard is None:
            raise LockNotOwnedError(name=self.name)
        released = await self._drop_lease(guard)
        del self._thread_guards[owner]
        if not released:
            raise LockNotOwnedError(name=self.name)

    async def do_thread_extend(self, owner: HolderIdentity) -> None:
        """Renew the read lease held by a worker thread.

        Raises:
            LockNotOwnedError: This thread holds no live read lease.
            LockAcquireError: The backend call failed.
        """
        guard = self._thread_guards.get(owner)
        if guard is None:
            raise LockNotOwnedError(name=self.name)
        await self.do_extend(guard)


class WriteMode(_Mode):
    """The write view of a `ReadWriteLock`.

    One writer holds it alone. A writer that finds readers in the way records
    an intent so readers arriving afterwards wait behind it.
    """

    __slots__ = ("_task_guards", "_thread_guards")

    def __init__(self, lock: ReadWriteLock) -> None:
        """Initialize the write view."""
        super().__init__(lock)
        self._task_guards: WeakKeyDictionary[
            asyncio.Task[object], WriteGuard
        ] = WeakKeyDictionary()
        self._thread_guards: WeakKeyDictionary[HolderIdentity, WriteGuard] = (
            WeakKeyDictionary()
        )
        self._thread_adapter: ThreadWriteAdapter | None = None

    async def __aenter__(self) -> WriteGuard:
        """Acquire the write lock.

        Raises:
            LockReentrantError: This task already holds the write lock.
            LockUpgradeError: This task holds the read lock.
            LockAcquireError: The backend call failed.
        """
        return await self.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release whatever this task holds, write lease or downgraded read.

        Raises:
            LockNotOwnedError: This task holds neither lease.
            LockReleaseError: The backend call failed.
        """
        task = self._running_task()
        if task in self._task_guards:
            await self.release()
            return
        await self._lock.read.release()

    @property
    def from_thread(self) -> ThreadWriteAdapter:
        """Return the write adapter for a worker thread."""
        if self._thread_adapter is None:
            self._thread_adapter = ThreadWriteAdapter(mode=self)
        return self._thread_adapter

    async def acquire(
        self,
        *,
        timeout: Annotated[  # noqa: ASYNC109
            Seconds | None,
            Doc(
                """
                Maximum number of seconds to wait for the lock.

                When None (the default), waits indefinitely. When set,
                retries until the deadline then raises LockTimeoutError and
                withdraws the intent.
                """,
            ),
        ] = None,
    ) -> WriteGuard:
        """Acquire the write lock, waiting for readers and writers to finish.

        Raises:
            LockReentrantError: This task already holds the write lock.
            LockUpgradeError: This task holds the read lock.
            LockAcquireError: The backend call failed.
            TimeoutError: `timeout` elapsed before the lock was granted.
        """
        task = self._running_task()
        self._assert_free(task)
        token = generate_task_token(self._lock._config.worker)  # noqa: SLF001
        duration = self._lock._config.lease_duration  # noqa: SLF001
        grant = await self._acquire_with_intent(
            token, duration=duration, timeout=timeout
        )
        guard = self._new_guard(token, grant, duration)
        self._task_guards[task] = guard
        return guard

    async def acquire_nowait(self) -> WriteGuard:
        """Acquire the write lock, without waiting and without an intent.

        A try that does not wait records no intent, so it never holds
        readers out.

        Raises:
            LockReentrantError: This task already holds the write lock.
            LockUpgradeError: This task holds the read lock.
            WouldBlockError: Readers or another writer hold the lock.
            LockAcquireError: The backend call failed.
        """
        task = self._running_task()
        self._assert_free(task)
        token = generate_task_token(self._lock._config.worker)  # noqa: SLF001
        duration = self._lock._config.lease_duration  # noqa: SLF001
        grant = await self.do_acquire(token, duration=duration, intent=False)
        if grant is None:
            msg = f"Write lock not acquired: name={self.name}, token={token}"
            raise WouldBlockError(msg)
        guard = self._new_guard(token, grant, duration)
        self._task_guards[task] = guard
        return guard

    async def extend(self) -> None:
        """Renew this task's write lease.

        Raises:
            LockNotOwnedError: This task no longer holds the write lock.
            LockAcquireError: The backend call failed.
        """
        await self.do_extend(self._guard_or_raise(self._running_task()))

    async def release(self) -> None:
        """Release this task's write lease.

        Raises:
            LockNotOwnedError: This task no longer holds the write lock.
            LockReleaseError: The backend call failed.
        """
        task = self._running_task()
        guard = self._guard_or_raise(task)
        released = await self._drop_lease(guard)
        del self._task_guards[task]
        if not released:
            raise LockNotOwnedError(name=self.name)

    async def owned(self) -> bool:
        """Return whether this task holds the live write lease.

        Raises:
            LockOwnedCheckError: The backend call failed.
        """
        guard = self._task_guards.get(self._running_task())
        if guard is None:
            return False
        backend = self.backend
        try:
            return await backend.owned_write(
                name=self._lock._lock_name,  # noqa: SLF001
                token=guard.token,
            )
        except Exception as exc:
            raise LockOwnedCheckError(name=self.name) from exc

    def _assert_free(self, task: asyncio.Task[object]) -> None:
        """Reject a nested acquire, and reject an upgrade from read to write.

        Raises:
            LockUpgradeError: The task holds the read lock.
            LockReentrantError: The task already holds the write lock.
        """
        if task in self._lock.read._task_guards:  # noqa: SLF001
            raise LockUpgradeError(name=self.name)
        if task in self._task_guards:
            raise LockReentrantError(name=self.name)

    def _guard_or_raise(self, holder: asyncio.Task[object]) -> WriteGuard:
        """Return the guard held by `holder`.

        Raises:
            LockNotOwnedError: The holder holds no write lease.
        """
        guard = self._task_guards.get(holder)
        if guard is None:
            raise LockNotOwnedError(name=self.name)
        return guard

    def _new_guard(
        self, token: str, grant: WriteGrant, duration: float
    ) -> WriteGuard:
        """Build a guard for a granted write lease."""
        return WriteGuard(
            owner=self,
            name=self.name,
            token=token,
            fencing_token=grant.fencing_token,
            poisoned=grant.poisoned,
            expires_at=monotonic() + duration,
        )

    async def _acquire_with_intent(
        self,
        token: str,
        *,
        duration: float,
        timeout: float | None,  # noqa: ASYNC109
    ) -> WriteGrant:
        """Retry until granted, withdrawing the intent if the wait ends badly.

        Raises:
            LockAcquireError: The backend call failed.
            TimeoutError: `timeout` elapsed before the lock was granted.
        """
        try:
            grant = await self._retry_until(
                lambda: self.do_acquire(token, duration=duration, intent=True),
                timeout=timeout,
            )
        except BaseException:
            with suppress(Exception):
                await self.backend.cancel_intent(
                    name=self._lock._lock_name,  # noqa: SLF001
                    token=token,
                )
            raise
        return grant

    async def _drop_lease(self, guard: WriteGuard) -> bool:
        """Drop a write lease on the backend and spend its guard.

        Returns whether the backend still held the lease.

        Raises:
            LockReleaseError: The backend call failed.
        """
        backend = self.backend
        try:
            released = await backend.release_write(
                name=self._lock._lock_name,  # noqa: SLF001
                token=guard.token,
            )
        except Exception as exc:
            raise LockReleaseError(name=self.name) from exc
        guard._invalidate()  # noqa: SLF001
        return released

    async def do_acquire(
        self, token: str, *, duration: float, intent: bool
    ) -> WriteGrant | None:
        """Ask the backend for a write lease.

        Raises:
            LockAcquireError: The backend call failed.
        """
        backend = self.backend
        try:
            return await backend.acquire_write(
                name=self._lock._lock_name,  # noqa: SLF001
                token=token,
                duration=duration,
                intent=intent,
            )
        except Exception as exc:
            raise LockAcquireError(name=self.name) from exc

    async def do_extend(self, guard: WriteGuard) -> None:
        """Renew the lease behind `guard`.

        A lease that was lost and retaken in the same call keeps the guard
        usable, with the new fencing token and `poisoned` set, because the
        writes made under the old token are now fenced out.

        Raises:
            LockNotOwnedError: The lease was lost to another holder.
            LockAcquireError: The backend call failed.
        """
        duration = self._lock._config.lease_duration  # noqa: SLF001
        grant = await self.do_acquire(
            guard.token, duration=duration, intent=False
        )
        if grant is None:
            guard._invalidate()  # noqa: SLF001
            raise LockNotOwnedError(name=self.name)
        guard._renewed(monotonic() + duration)  # noqa: SLF001
        if grant.fencing_token != guard._fencing_token:  # noqa: SLF001
            guard._fencing_token = grant.fencing_token  # noqa: SLF001
            guard._poisoned = True  # noqa: SLF001

    async def do_downgrade(self, guard: WriteGuard) -> ReadGuard:
        """Turn the write lease behind `guard` into a read lease.

        Raises:
            LockNotOwnedError: This holder no longer holds the write lock.
            LockAcquireError: The backend call failed.
        """
        task = self._running_task()
        duration = self._lock._config.lease_duration  # noqa: SLF001
        backend = self.backend
        try:
            generation = await backend.downgrade(
                name=self._lock._lock_name,  # noqa: SLF001
                token=guard.token,
                duration=duration,
            )
        except Exception as exc:
            raise LockAcquireError(name=self.name) from exc
        if generation is None:
            guard._invalidate()  # noqa: SLF001
            self._task_guards.pop(task, None)
            raise LockNotOwnedError(name=self.name)
        guard._invalidate()  # noqa: SLF001
        self._task_guards.pop(task, None)
        read_guard = self._lock.read._new_guard(  # noqa: SLF001
            guard.token, generation, duration
        )
        self._lock.read._adopt(read_guard, task)  # noqa: SLF001
        return read_guard

    async def do_thread_acquire(
        self,
        owner: HolderIdentity,
        *,
        timeout: Seconds | None = None,  # noqa: ASYNC109
    ) -> WriteGuard:
        """Acquire the write lock for a worker thread.

        Raises:
            LockReentrantError: This thread already holds the write lock.
            LockUpgradeError: This thread holds the read lock.
            LockAcquireError: The backend call failed.
            TimeoutError: `timeout` elapsed before the lock was granted.
        """
        if owner in self._lock.read._thread_guards:  # noqa: SLF001
            raise LockUpgradeError(name=self.name)
        if owner in self._thread_guards:
            raise LockReentrantError(name=self.name)
        config = self._lock._config  # noqa: SLF001
        token = generate_thread_token(config.worker, identity=owner)
        duration = config.lease_duration
        grant = await self._acquire_with_intent(
            token, duration=duration, timeout=timeout
        )
        guard = self._new_guard(token, grant, duration)
        self._thread_guards[owner] = guard
        return guard

    async def do_thread_release(self, owner: HolderIdentity) -> None:
        """Release the write lease held by a worker thread.

        Raises:
            LockNotOwnedError: This thread holds no live write lease.
            LockReleaseError: The backend call failed.
        """
        guard = self._thread_guards.get(owner)
        if guard is None:
            raise LockNotOwnedError(name=self.name)
        released = await self._drop_lease(guard)
        del self._thread_guards[owner]
        if not released:
            raise LockNotOwnedError(name=self.name)

    async def do_thread_extend(self, owner: HolderIdentity) -> None:
        """Renew the write lease held by a worker thread.

        Raises:
            LockNotOwnedError: This thread holds no live write lease.
            LockAcquireError: The backend call failed.
        """
        guard = self._thread_guards.get(owner)
        if guard is None:
            raise LockNotOwnedError(name=self.name)
        await self.do_extend(guard)


class _ThreadAdapter[ModeT: _Mode]:
    """Dispatch a mode's coroutines onto the backend's event loop."""

    __slots__ = ("_mode",)

    _view: ClassVar[str]
    """Name of the view this adapter serves, `read` or `write`."""

    def __init__(self, mode: ModeT) -> None:
        """Initialize the adapter."""
        self._mode = mode

    @property
    def _backend_loop(self) -> asyncio.AbstractEventLoop:
        """Return the event loop the backend captured on `__aenter__`."""
        loop = self._mode.backend._loop  # noqa: SLF001
        if loop is None:
            raise_backend_not_open(f"ReadWriteLock {self._mode.name!r}")
        if on_backend_loop(loop):
            view = self._view
            raise_event_loop_deadlock(
                f"ReadWriteLock {self._mode.name!r} `{view}.from_thread`",
                f"Use `async with lock.{view}:` from async code, or run the "
                "sync call through `asyncio.to_thread(...)`.",
            )
        return loop


class ThreadReadAdapter(_ThreadAdapter[ReadMode]):
    """Read adapter for a worker thread spawned from an event loop."""

    __slots__ = ()

    _view = "read"

    def __enter__(self) -> ReadGuard:
        """Acquire the read lock."""
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the read lock."""
        self.release()

    def acquire(self, *, timeout: Seconds | None = None) -> ReadGuard:
        """Acquire the read lock, blocking this thread."""
        loop = self._backend_loop
        return asyncio.run_coroutine_threadsafe(
            self._mode.do_thread_acquire(
                current_thread_identity(), timeout=timeout
            ),
            loop,
        ).result()

    def extend(self) -> None:
        """Renew this thread's read lease."""
        loop = self._backend_loop
        asyncio.run_coroutine_threadsafe(
            self._mode.do_thread_extend(current_thread_identity()),
            loop,
        ).result()

    def release(self) -> None:
        """Release this thread's read lease."""
        loop = self._backend_loop
        asyncio.run_coroutine_threadsafe(
            self._mode.do_thread_release(current_thread_identity()),
            loop,
        ).result()


class ThreadWriteAdapter(_ThreadAdapter[WriteMode]):
    """Write adapter for a worker thread spawned from an event loop."""

    __slots__ = ()

    _view = "write"

    def __enter__(self) -> WriteGuard:
        """Acquire the write lock."""
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the write lock."""
        self.release()

    def acquire(self, *, timeout: Seconds | None = None) -> WriteGuard:
        """Acquire the write lock, blocking this thread."""
        loop = self._backend_loop
        return asyncio.run_coroutine_threadsafe(
            self._mode.do_thread_acquire(
                current_thread_identity(), timeout=timeout
            ),
            loop,
        ).result()

    def extend(self) -> None:
        """Renew this thread's write lease."""
        loop = self._backend_loop
        asyncio.run_coroutine_threadsafe(
            self._mode.do_thread_extend(current_thread_identity()),
            loop,
        ).result()

    def release(self) -> None:
        """Release this thread's write lease."""
        loop = self._backend_loop
        asyncio.run_coroutine_threadsafe(
            self._mode.do_thread_release(current_thread_identity()),
            loop,
        ).result()
