"""Task Lock.

A distributed lock for scheduled tasks with two time boundaries:
- min_hold_duration: Prevents re-execution on other nodes after task completes.
- lease_duration: Auto-expires the lock (deadlock protection).
"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import Annotated, Final, Self
from uuid import UUID

from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._app import resolve_ambient
from grelmicro._async import (
    on_backend_loop,
    raise_backend_not_open,
    raise_event_loop_deadlock,
    run_to_completion,
)
from grelmicro._config import (
    Reconfigurable,
    default_env_prefix,
    env_prefixes,
    kind_env_prefix,
    resolve_config,
)
from grelmicro._duration import Duration
from grelmicro._environment import record_coordination
from grelmicro.coordination._base import (
    BaseLockConfig,
    assert_worker_unchanged,
)
from grelmicro.coordination._metrics import (
    ACQUIRED,
    ERROR,
    LOST,
    SUCCESS,
    UNAVAILABLE,
    LockMetrics,
)
from grelmicro.coordination._protocol import LockBackend, LockPrimitive
from grelmicro.coordination._tokens import (
    generate_task_token,
    generate_thread_token,
    generate_token_nonce,
)
from grelmicro.coordination.errors import (
    LockAcquireError,
    LockExtendError,
    LockLockedCheckError,
    LockNotOwnedError,
    LockReentrantError,
    LockReleaseError,
)
from grelmicro.coordination.lock import validate_lock_name
from grelmicro.errors import (
    SettingsValidationError,
    WouldBlockError,
)

logger = getLogger("grelmicro.coordination")


_NO_BACKEND: Final = (
    "TaskLock({name!r}) resolved no backend.",
    (
        "Register a Coordination component, pass backend=, or run the call "
        "inside `async with micro:`."
    ),
)
"""What `backend` raises when no `backend=` was passed and none resolves.

The lead names the miss, and the fix is given when no app is bound.
"""

_NO_NAME: Final = (
    "TaskLock has no name. Pass a name, such as TaskLock('cleanup'), "
    "or use it as the gate of a task, where it takes the task name."
)
"""What a `TaskLock` without a name raises when it is used."""


class TaskLockConfig(BaseLockConfig):
    """Task Lock Config."""

    min_hold_duration: Annotated[
        Duration,
        Doc(
            """
            The minimum duration to hold the lock after task completion,
            in whole seconds or as a `timedelta`.

            Prevents re-execution on other nodes before this duration has
            elapsed. A float is refused. From text, such as an
            environment variable, it reads whole seconds (`"1"`) or an
            ISO 8601 duration (`"PT0.5S"`).
            """
        ),
    ] = timedelta(seconds=1)
    lease_duration: Annotated[
        Duration,
        Doc(
            """
            The maximum duration to hold the lock (deadlock protection),
            in whole seconds or as a `timedelta`.

            Acts as the TTL on acquire. A float is refused. From text it
            reads like `min_hold_duration`.
            """
        ),
    ] = timedelta(seconds=60)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.min_hold_duration > self.lease_duration:
            msg = (
                "min_hold_duration must be less than or equal to lease_duration"
            )
            raise ValueError(msg)
        return self


class TaskLock(Reconfigurable[TaskLockConfig], LockPrimitive):
    """Task Lock.

    A distributed lock for scheduled tasks. Unlike a regular Lock,
    TaskLock does not release immediately on context manager exit. Instead, it keeps
    the lock held for at least `min_hold_duration` to prevent re-execution
    on other nodes.

    This lock is designed to be used as the `gate` of `@tasks.every`. There,
    the task extends the lease every third of `lease_duration` from the moment
    it holds the lock until the body ends, so `lease_duration` only bounds how
    long a crashed worker keeps it. Entered directly with `async with`, the
    lock extends nothing and relies on the TTL set at acquire time. Call
    `extend()` from a long body to push the lease further out.

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
            str | None,
            Doc(
                """
                The name of the resource to lock.

                It will be used as the lock name so make sure it is unique on the lock backend.

                When used as the `gate` of `@tasks.every`, a lock without a
                name takes the task name, so it does not need to be
                repeated. A lock without a name used on its own raises
                `SettingsValidationError` when it is first used.
                """
            ),
        ] = None,
        *,
        backend: Annotated[
            LockBackend | str | None,
            Doc(
                """
                The distributed lock backend used to acquire and release the lock.

                By default, it resolves the lock backend from the
                active app's `Coordination` component.
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
        min_hold_duration: Annotated[
            int | timedelta | None,
            Doc(
                """
                The minimum duration to hold the lock after task completion,
                in whole seconds or as a `timedelta`. A float is refused.

                Default: 1. Prevents re-execution on other nodes
                before this duration has elapsed. When unset and env reads
                are enabled (see `env_load` and `GREL_ENV_LOAD`),
                resolves from the environment variable
                `GREL_TASKLOCK_MIN_HOLD_DURATION` for a lock without a
                name (`GREL_TASKLOCK_{NAME_UPPER}_MIN_HOLD_DURATION`
                for a named one) if present, otherwise falls back to the
                `TaskLockConfig` default.
                """
            ),
        ] = None,
        lease_duration: Annotated[
            int | timedelta | None,
            Doc(
                """
                The maximum duration to hold the lock (deadlock protection),
                in whole seconds or as a `timedelta`. A float is refused.

                Default: 60. Acts as the TTL on acquire. When unset and env reads
                are enabled (see `env_load` and `GREL_ENV_LOAD`),
                resolves from the environment variable
                `GREL_TASKLOCK_LEASE_DURATION` for a lock without a name
                (`GREL_TASKLOCK_{NAME_UPPER}_LEASE_DURATION` for a named
                one) if present, otherwise falls back to the
                `TaskLockConfig` default.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_TASKLOCK_` for a lock without a name,
                `GREL_TASKLOCK_{NAME_UPPER}_` for a named one. Set this
                to a custom prefix when the application uses a different
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

                Pass False when the values here are the whole truth.
                Env reads fill every field not passed, so a config
                half taken from somewhere else silently gets the rest
                from the environment.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the task lock."""
        resolved_env_prefix, kind_prefix = (
            env_prefixes("TASKLOCK", name, env_prefix)
            if name is not None
            else (env_prefix or kind_env_prefix("TASKLOCK"), None)
        )
        config = resolve_config(
            TaskLockConfig,
            explicit=None,
            kwargs={
                "worker": worker,
                "min_hold_duration": min_hold_duration,
                "lease_duration": lease_duration,
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
            str | None,
            Doc(
                """
                The name of the resource to lock.

                Acts as the instance identity. Used as the backend
                lock key and exposed via the `name` property. Pass
                `None` for a lock that takes the task name as the `gate`
                of `@tasks.every`.
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

                By default, it resolves the lock backend from the
                active app's `Coordination` component.
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
        name: str | None,
        config: TaskLockConfig,
        backend: LockBackend | str | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name: str | None = None
        if name is not None:
            self._set_name(name)
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._backend: LockBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )
        if self._backend is not None:
            record_coordination(self, self._backend, "lock")
        self._acquired_at: float | None = None
        self._token_nonce = generate_token_nonce()
        # The nonce and local end of the hold this instance last set, so it
        # can take the hold back once it ran out (see `_take_back_hold`).
        self._hold_nonce: str | None = None
        self._held_token: str | None = None
        self._hold_ends = 0.0
        self._from_thread: ThreadTaskLockAdapter | None = None
        self._task_name: str | None = None
        self._min_hold_floor = timedelta(0)

    def _set_name(self, name: str) -> None:
        """Name the lock and the metrics derived from it.

        Raises:
            SettingsValidationError: If `name` is not a valid lock name.
        """
        validate_lock_name(name)
        self._name = name
        self._metrics = LockMetrics(name, "task")

    @property
    def _key(self) -> str:
        """Return the backend lock key, refusing a lock without a name.

        Raises:
            SettingsValidationError: If the lock has no name.
        """
        return f"{self._LOCK_PREFIX}:{self._named}"

    @property
    def _named(self) -> str:
        """Return the lock name, refusing a lock without one.

        Raises:
            SettingsValidationError: If the lock has no name.
        """
        if self._name is None:
            raise SettingsValidationError(_NO_NAME)
        return self._name

    def _bind_task(
        self, task_name: str, *, lock_name: str, interval: timedelta
    ) -> None:
        """Bind the lock to the one interval task it gates.

        A lock without a name takes ``lock_name``, the task name as a
        valid lock name. The rename happens in place, so the handle the
        caller holds is the lock the task enters, and an external reload
        reads it under the task name, ``GREL_TASKLOCK_{TASK}_``, instead of
        the prefix every lock without a name shares. From then on, every
        config the lock takes must hold a claim for at least ``interval``,
        a later `reconfigure` included.

        Raises:
            ValueError: If the lock already gates another task.
            SettingsValidationError: If `min_hold_duration` is shorter
                than ``interval``, or the lock takes a ``lock_name`` that
                is not a valid lock name.
        """
        if self._task_name is not None:
            msg = (
                f"TaskLock {self._name!r} already gates task "
                f"{self._task_name!r}, give each task its own TaskLock"
            )
            raise ValueError(msg)
        _check_min_hold(self._config, interval)
        if self._name is None:
            self._set_name(lock_name)
            # A tracked lock on the shared prefix of locks without a name
            # reloads under its task name from now on, like a lock named
            # after it. A name no env var can spell leaves it out of
            # external reload instead.
            if getattr(self, "_env_prefix", None) == kind_env_prefix(
                "TASKLOCK"
            ):
                env_prefix: str | None = None
                with suppress(SettingsValidationError):
                    env_prefix = default_env_prefix("TASKLOCK", task_name)
                self._env_prefix = env_prefix
        self._min_hold_floor = interval
        self._task_name = task_name

    @property
    def name(self) -> str | None:
        """Return the task lock identity, or `None` for a lock without a name."""
        return self._name

    @property
    def backend(self) -> LockBackend:
        """Bound lock backend, resolved on each call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the active `Grelmicro` app is
        consulted on every access so that
        `micro.override(Coordination(...))` blocks take effect.

        Raises:
            OutOfContextError: No backend resolved in this scope.
                Register a `Coordination` Component, pass `backend=`,
                or run the call inside `async with micro:`.
                `micro.install(app)` covers request and message handlers,
                and not a lifespan of your own.
        """
        if self._backend is not None:
            return self._backend
        return resolve_ambient(
            ("coordination", self._backend_name or "default"),
            _NO_BACKEND,
            self._name,
        ).lock_backend

    async def __aenter__(self) -> Self:
        """Acquire the lock with duration=lease_duration.

        Raises:
            WouldBlockError: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            SettingsValidationError: If the lock has no name.
        """
        config = self._config
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._named)

        self._take_back_hold()
        token = generate_task_token(config.worker, self._token_nonce)
        if not await self.do_acquire(token, duration=config.lease_duration):
            msg = f"Task lock not acquired: name={self._named}, token={token}"
            raise WouldBlockError(msg)
        self._held_token = token

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Release or extend the lock based on elapsed time.

        If elapsed >= min_hold_duration, release immediately.
        If elapsed < min_hold_duration, re-acquire with remaining duration (let TTL expire).

        Raises:
            LockNotOwnedError: If the lock is not held or its lease ran out.
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        config = self._config
        token = generate_task_token(config.worker, self._token_nonce)
        # The release or the shortened hold runs to its end even when
        # the task is cancelled.
        await run_to_completion(
            self.do_exit(token, min_hold_duration=config.min_hold_duration)
        )
        return None

    @property
    def from_thread(self) -> ThreadTaskLockAdapter:
        """Return the task lock adapter for worker thread."""
        if self._from_thread is None:
            self._from_thread = ThreadTaskLockAdapter(task_lock=self)
        return self._from_thread

    async def extend(self) -> None:
        """Extend the lease for another `lease_duration` without releasing.

        Raises:
            LockNotOwnedError: If this task does not hold the lock or the lease was lost.
            LockExtendError: If the backend call fails.
        """
        if self._acquired_at is None:
            raise LockNotOwnedError(name=self._named)
        token = generate_task_token(self._config.worker, self._token_nonce)
        if not await self._extend_lease(token):
            raise LockNotOwnedError(name=self._named)

    async def _extend_held(self) -> None:
        """Extend the lease the lock holds, from any asyncio task.

        `extend` answers only in the task that entered the lock. The
        scheduler extends a claim from a task of its own while the body
        runs, with the token captured on entry.

        Raises:
            LockNotOwnedError: If the lock is not held or the lease was lost.
            LockExtendError: If the backend call fails.
        """
        token = self._held_token
        if token is None or not await self._extend_lease(token):
            raise LockNotOwnedError(name=self._named)

    async def locked(self) -> bool:
        """Check if the lock is acquired.

        Raises:
            LockLockedCheckError: If the lock cannot be checked due to an error on the backend.
            SettingsValidationError: If the lock has no name.
        """
        key = self._key
        backend = self.backend
        try:
            return await backend.locked(name=key)
        except Exception as exc:
            raise LockLockedCheckError(name=self._named) from exc

    async def do_acquire(self, token: str, *, duration: timedelta) -> bool:
        """Acquire the lock.

        This method should not be called directly. Use the context manager instead.

        Args:
            token: The token to register on the backend.
            duration: The lease duration to request. The
                caller captures this from
                `self._config.lease_duration` at the start of the
                operation so a concurrent `reconfigure` cannot change
                the duration mid-acquire.

        Returns:
            bool: True if the lock was acquired, False if the lock was not acquired.

        Raises:
            LockAcquireError: If the lock cannot be acquired due to an error on the backend.
        """
        key = self._key
        backend = self.backend
        try:
            # TaskLock does not surface the fencing token. A non-None result
            # means the lock was acquired.
            fencing_token = await backend.acquire(
                name=key,
                token=token,
                duration=duration,
            )
        except Exception as exc:
            self._metrics.attempt(ERROR)
            raise LockAcquireError(name=self._named) from exc
        acquired = fencing_token is not None
        if acquired:
            self._hold_nonce = None
            self._acquired_at = monotonic()
            self._metrics.attempt(ACQUIRED)
            self._metrics.hold(1)
        else:
            self._metrics.attempt(UNAVAILABLE)
        return acquired

    async def do_release(self, token: str) -> bool:
        """Release the lock.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was released, False otherwise.

        Raises:
            LockReleaseError: Cannot release the lock due to backend error.
        """
        key = self._key
        backend = self.backend
        try:
            # Past the lease the backend frees the lock on its own.
            async with asyncio.timeout(
                self._config.lease_duration.total_seconds()
            ):
                return await backend.release(name=key, token=token)
        except Exception as exc:
            raise LockReleaseError(name=self._named) from exc

    async def do_reacquire(self, token: str, duration: timedelta) -> bool:
        """Re-acquire the lock with a specific duration.

        This method should not be called directly. Use the context manager instead.

        Returns:
            bool: True if the lock was re-acquired, False otherwise.

        Raises:
            LockReleaseError: Cannot re-acquire the lock due to backend error.
        """
        return await self._ask_backend(token, duration, LockReleaseError)

    async def _extend_lease(self, token: str) -> bool:
        """Extend the lease held with `token` for another `lease_duration`.

        Returns:
            bool: True if the lease was extended, False if it was lost.

        Raises:
            LockExtendError: Cannot extend the lease due to backend error.
        """
        return await self._ask_backend(
            token, self._config.lease_duration, LockExtendError
        )

    async def _ask_backend(
        self,
        token: str,
        duration: timedelta,
        error: type[LockReleaseError | LockExtendError],
    ) -> bool:
        """Ask the backend to hold the lock for `duration`, counting the outcome.

        Returns:
            bool: True if the backend holds the lock for `token`, False otherwise.

        Raises:
            LockReleaseError: The backend call failed and `error` is it.
            LockExtendError: The backend call failed and `error` is it.
        """
        key = self._key
        backend = self.backend
        try:
            # TaskLock does not surface the fencing token. A non-None result
            # means the lock is held. Past the lease the answer is moot.
            async with asyncio.timeout(
                self._config.lease_duration.total_seconds()
            ):
                held = (
                    await backend.acquire(
                        name=key,
                        token=token,
                        duration=duration,
                    )
                ) is not None
        except Exception as exc:
            self._metrics.extension(ERROR)
            raise error(name=self._named) from exc
        self._metrics.extension(SUCCESS if held else LOST)
        return held

    async def do_thread_enter(self) -> None:
        """Acquire the lock from a worker thread.

        Runs entirely on the event loop so the reentrant check, token
        generation, and backend acquire are atomic with respect to other
        threads.

        Raises:
            WouldBlockError: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            SettingsValidationError: If the lock has no name.
        """
        config = self._config
        if self._acquired_at is not None:
            raise LockReentrantError(name=self._named)

        self._take_back_hold()
        token = generate_thread_token(config.worker, self._token_nonce)
        if not await self.do_acquire(token, duration=config.lease_duration):
            msg = f"Task lock not acquired: name={self._named}, token={token}"
            raise WouldBlockError(msg)
        self._held_token = token

    async def do_thread_exit(self) -> None:
        """Release or extend the lock from a worker thread.

        Runs entirely on the event loop so the token generation and backend
        release are atomic with respect to other threads.

        Raises:
            LockNotOwnedError: If the lock is not held or its lease ran out.
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        config = self._config
        token = generate_thread_token(config.worker, self._token_nonce)
        await self.do_exit(token, min_hold_duration=config.min_hold_duration)

    async def _apply_reconfigure(self, new_config: TaskLockConfig) -> None:
        """Validate `new_config` before publishing it.

        The `worker` field is immutable, and a lock gating an interval
        task keeps holding a claim for the whole interval.
        """
        assert_worker_unchanged(self._config, new_config)
        _check_min_hold(new_config, self._min_hold_floor)

    def _take_back_hold(self) -> None:
        """Enter with the token of this instance's own hold once it ran out.

        A backend that rounds lease times up keeps a hold
        past `min_hold_duration`. Once the hold ran out on this
        instance's clock, reusing its token lets the instance that set
        it through, while every other holder still waits for the
        backend to expire it.
        """
        if self._hold_nonce is not None and monotonic() >= self._hold_ends:
            self._token_nonce = self._hold_nonce

    async def do_exit(
        self, token: str, *, min_hold_duration: timedelta
    ) -> None:
        """Handle exit logic: release or re-acquire based on elapsed time.

        Args:
            token: The token used to release or re-acquire the lock.
            min_hold_duration: The minimum hold duration to enforce. The
                caller captures this from
                `self._config.min_hold_duration` at the start of the
                operation so the comparison and the
                remaining-duration calculation always agree.

        Raises:
            LockNotOwnedError: If the lock is not held or its lease ran out.
            LockReleaseError: If the lock cannot be released or re-acquired
                due to a backend error.
        """
        if self._acquired_at is None:
            raise LockNotOwnedError(name=self._named)

        elapsed = timedelta(seconds=monotonic() - self._acquired_at)
        self._acquired_at = None
        self._held_token = None
        nonce = self._token_nonce
        self._token_nonce = generate_token_nonce()
        self._metrics.hold(-1)

        if elapsed >= min_hold_duration:
            # Task took longer than min_hold_duration, release immediately
            released = await self.do_release(token)
            if not released:
                raise LockNotOwnedError(name=self._named)
        else:
            # Re-acquire with remaining duration so the lock is held
            # until min_hold_duration.
            remaining = min_hold_duration - elapsed
            re_acquired = await self.do_reacquire(token, remaining)
            if not re_acquired:
                raise LockNotOwnedError(name=self._named)
            self._hold_nonce = nonce
            self._hold_ends = monotonic() + remaining.total_seconds()


def _check_min_hold(config: TaskLockConfig, interval: timedelta) -> None:
    """Refuse a config that holds a claim for less than `interval`.

    `TaskLockConfig` keeps `lease_duration` at or above
    `min_hold_duration`, so the lease covers the interval too.

    Raises:
        SettingsValidationError: If `min_hold_duration` is shorter than
            `interval`.
    """
    if config.min_hold_duration < interval:
        msg = (
            "min_hold_duration must be greater than or equal to interval,"
            " or a peer claims the same interval once the body ends"
        )
        raise SettingsValidationError(msg)


class ThreadTaskLockAdapter:
    """Task Lock Adapter for Worker Thread."""

    def __init__(self, task_lock: TaskLock) -> None:
        """Initialize the task lock adapter."""
        self._task_lock = task_lock

    @property
    def _backend_loop(self) -> asyncio.AbstractEventLoop:
        """Return the event loop the backend captured on ``__aenter__``.

        Raises:
            SettingsValidationError: If the lock has no name.
        """
        name = self._task_lock._named  # noqa: SLF001
        loop = self._task_lock.backend._loop  # noqa: SLF001
        if loop is None:
            raise_backend_not_open(f"TaskLock {name!r}")
        if on_backend_loop(loop):
            raise_event_loop_deadlock(
                f"TaskLock {name!r} `from_thread`",
                "Use `async with task_lock:` from async code, or run the "
                "sync call through `asyncio.to_thread(...)`.",
            )
        return loop

    def __enter__(self) -> Self:
        """Acquire the task lock with the context manager.

        Raises:
            WouldBlockError: If the lock is already held by another worker.
            LockAcquireError: If the lock cannot be acquired due to a backend error.
            LockReentrantError: If the lock is already acquired (nested usage is not supported).
            SettingsValidationError: If the lock has no name.
        """
        loop = self._backend_loop
        asyncio.run_coroutine_threadsafe(
            self._task_lock.do_thread_enter(),
            loop,
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
            LockNotOwnedError: If the lock is not held or its lease ran out.
            LockReleaseError: If the lock cannot be released due to a backend error.
        """
        loop = self._backend_loop
        asyncio.run_coroutine_threadsafe(
            self._task_lock.do_thread_exit(),
            loop,
        ).result()

    def locked(self) -> bool:
        """Return True if the lock is currently held."""
        loop = self._backend_loop
        return asyncio.run_coroutine_threadsafe(
            self._task_lock.locked(),
            loop,
        ).result()
