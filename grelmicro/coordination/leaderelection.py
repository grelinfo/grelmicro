"""Leader Election."""

import asyncio
from collections.abc import Mapping
from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import Annotated, Self
from uuid import UUID

from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._app import Grelmicro
from grelmicro._async import sleep_or_stop
from grelmicro._config import Reconfigurable, env_segment, resolve_config
from grelmicro.coordination._base import BaseLockConfig
from grelmicro.coordination.abc import (
    LeaderElectionBackend,
    LeaderRecord,
    LockPrimitive,
    Seconds,
)
from grelmicro.errors import WouldBlockError
from grelmicro.task.abc import Task

logger = getLogger("grelmicro.leader_election")


class LeaderElectionConfig(BaseLockConfig, frozen=True, extra="forbid"):  # ty: ignore[invalid-frozen-dataclass-subclass]
    """Leader Election Config.

    Leader election based on a leased reentrant distributed lock.
    """

    lease_duration: Annotated[
        Seconds,
        Doc(
            """
            The lease duration in seconds.
            """,
        ),
    ] = 15
    renew_deadline: Annotated[
        Seconds,
        Doc(
            """
            The renew deadline in seconds.
            """,
        ),
    ] = 10
    retry_interval: Annotated[
        Seconds,
        Doc(
            """
            The retry interval in seconds.
            """,
        ),
    ] = 2
    backend_timeout: Annotated[
        Seconds,
        Doc(
            """
            The backend timeout in seconds.
            """,
        ),
    ] = 5
    error_interval: Annotated[
        Seconds,
        Doc(
            """
            The error interval in seconds.
            """,
        ),
    ] = 30

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.renew_deadline >= self.lease_duration:
            msg = "Renew deadline must be shorter than lease duration"
            raise ValueError(msg)
        if self.retry_interval >= self.renew_deadline:
            msg = "Retry interval must be shorter than renew deadline"
            raise ValueError(msg)
        if self.backend_timeout >= self.renew_deadline:
            msg = "Backend timeout must be shorter than renew deadline"
            raise ValueError(msg)
        return self


class LeaderElection(Reconfigurable[LeaderElectionConfig], LockPrimitive, Task):
    """Leader Election.

    The leader election is a synchronization primitive with the worker as scope.
    It runs as a task to acquire or renew the distributed lock.

    Supports live reconfiguration via
    `reconfigure(new_config)`.
    A swap takes effect on the next renew loop iteration. The
    `worker` field cannot change. Changing it raises `ValueError`.
    See [Live reconfiguration](../architecture/reconfigure.md).
    """

    _LOCK_PREFIX = "leader"

    def __init__(  # noqa: PLR0913
        self,
        name: Annotated[
            str,
            Doc(
                """
                The name of the resource representing the leader election.

                It will be used as the lock name so make sure it is unique on the distributed lock
                backend.
                """,
            ),
        ],
        *,
        backend: Annotated[
            LeaderElectionBackend | str | None,
            Doc(
                """
                The leader election backend used to acquire and renew leadership.

                By default, it resolves the `Coordination` component from the active app.
                """,
            ),
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
            Seconds | None,
            Doc(
                """
                The duration in seconds after the lock will be released if not renewed.

                Default: 15. If the worker becomes unavailable, the lock
                can only be acquired by another worker after it has
                expired. When unset and env reads are enabled (see ``env_load`` and
                ``GREL_ENV_LOAD``), resolves from the environment
                variable
                `GREL_LEADER_ELECTION_{NAME_UPPER}_LEASE_DURATION` if
                present, otherwise falls back to the
                `LeaderElectionConfig` default.
                """,
            ),
        ] = None,
        renew_deadline: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds that the leader worker will try to acquire the lock before
                giving up.

                Default: 10. Must be shorter than the lease duration.
                In case of multiple errors, the leader worker will
                lose the lead to prevent split-brain scenarios and
                ensure that only one worker is the leader at any
                time.
                """,
            ),
        ] = None,
        retry_interval: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds between attempts to acquire or renew the lock.

                Default: 2. Must be shorter than the renew deadline.
                A shorter schedule enables faster leader elections
                but may increase load on the distributed lock
                backend, while a longer schedule reduces load but
                can delay new leader elections.
                """,
            ),
        ] = None,
        backend_timeout: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds for waiting on backend for acquiring and releasing the lock.

                Default: 5. This value determines how long the system
                will wait before giving up the current operation.
                """,
            ),
        ] = None,
        error_interval: Annotated[
            Seconds | None,
            Doc(
                """
                The duration in seconds between logging error messages.

                Default: 30. If shorter than the retry interval, it
                will log every error. It is used to prevent flooding
                the logs when the lock backend is unavailable.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: `GREL_LEADER_ELECTION_{NAME_UPPER}_`. Set
                this to a custom prefix when the application uses a
                different naming convention.
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
        metadata: Annotated[
            Mapping[str, str] | None,
            Doc(
                """
                Free-form key/value pairs stored on the lease while this worker
                leads, for observability (pod name, version, region). Other
                workers read them via `LeaderElection.record`.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the leader election."""
        config = resolve_config(
            LeaderElectionConfig,
            explicit=None,
            kwargs={
                "worker": worker,
                "lease_duration": lease_duration,
                "renew_deadline": renew_deadline,
                "retry_interval": retry_interval,
                "backend_timeout": backend_timeout,
                "error_interval": error_interval,
            },
            env_prefix=env_prefix
            or f"GREL_LEADER_ELECTION_{env_segment(name)}_",
            env_load=env_load,
        )
        self._setup(name, config, backend, metadata)

    @classmethod
    def from_config(
        cls,
        name: Annotated[
            str,
            Doc(
                """
                The name of the resource representing the leader election.

                Acts as the instance identity. Used as the backend
                lock key and exposed via the `name` property.
                """,
            ),
        ],
        config: Annotated[
            LeaderElectionConfig,
            Doc(
                """
                The pre-built leader election configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a `pydantic-settings` aggregator). The environment
                path is bypassed and the config is used as-is.
                """,
            ),
        ],
        *,
        backend: Annotated[
            LeaderElectionBackend | str | None,
            Doc(
                """
                The leader election backend used to acquire and renew leadership.

                By default, it resolves the `Coordination` component from the active app.
                """,
            ),
        ] = None,
        metadata: Annotated[
            Mapping[str, str] | None,
            Doc(
                """
                Free-form key/value pairs stored on the lease while this worker
                leads, for observability.
                """,
            ),
        ] = None,
    ) -> Self:
        """Construct a `LeaderElection` from a name and a pre-built `LeaderElectionConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend, metadata)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: LeaderElectionConfig,
        backend: LeaderElectionBackend | str | None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._metadata: dict[str, str] = dict(metadata or {})
        self._record: LeaderRecord | None = None
        self._reconfigure_lock = asyncio.Lock()
        self._lock_name = f"{self._LOCK_PREFIX}:{name}"
        self._backend: LeaderElectionBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )

        self._service_running = False
        self._state_change_condition: asyncio.Condition = asyncio.Condition()
        self._is_leader: bool = False
        self._state_updated_at: float = monotonic()
        # Monotonic timestamp of the last backend response that
        # confirmed this worker still holds leadership. `None` until
        # the first acquisition succeeds. Reset to `None` when
        # leadership is lost. Drives `is_leader_confirmed_within()`.
        self._last_confirmed_at: float | None = None
        self._error_logged_at: float | None = None

    async def __aenter__(self) -> Self:
        """Wait for the leader with the context manager."""
        await self.wait_for_leader()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the context manager (no-op)."""
        return None

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    @property
    def record(self) -> LeaderRecord | None:
        """The most recent `LeaderRecord` seen from the backend.

        `None` until the first acquire/renew attempt completes. Reflects the
        current leader (`record.holder`), when they acquired and renewed the
        lease, how many times leadership has changed, and the holder's
        metadata. Updated on every renew loop iteration.
        """
        return self._record

    @property
    def backend(self) -> LeaderElectionBackend:
        """Bound coordination backend, resolved on each call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the active `Grelmicro` app is
        consulted via `Grelmicro.current()` on every access so that
        `micro.override(Coordination(...))` blocks take effect. The
        backend comes from the `Coordination` component, whose election
        backend can point at a different vendor than its lock backend.
        """
        if self._backend is not None:
            return self._backend
        coordination = Grelmicro.current().get(
            "coordination", self._backend_name or "default"
        )
        return coordination.election_backend

    def is_running(self) -> bool:
        """Check if the leader election task is running."""
        return self._service_running

    def is_leader(self) -> bool:
        """Check if the current worker is the leader.

        This is an **advisory** local view. The result reflects the
        last backend response plus the configured `renew_deadline`.
        During a backend partition the answer can remain ``True``
        until the renew deadline elapses, even if another worker has
        already acquired leadership through a reachable backend.

        For work that cannot tolerate stale local leadership, use
        [`is_leader_confirmed_within`][grelmicro.coordination.LeaderElection.is_leader_confirmed_within]
        with a tighter freshness bound, or fence each backend write
        with the lock token.

        Returns:
            True if the current worker is the leader (subject to the
            uncertainty window described above), False otherwise.

        """
        if not self._is_leader:
            return False
        return not self._is_renew_deadline_reached(self._config)

    def last_confirmation_age(self) -> float | None:
        """Seconds since the last backend response that confirmed leadership.

        Returns ``None`` until the first acquisition succeeds, and is
        reset to ``None`` whenever leadership is lost. The value
        grows during a backend partition because the underlying
        timestamp is only refreshed when the backend responds with
        "you still hold the lock".
        """
        if self._last_confirmed_at is None:
            return None
        return monotonic() - self._last_confirmed_at

    def is_leader_confirmed_within(self, max_age: float) -> bool:
        """Stricter than `is_leader`: require a recent backend confirmation.

        Returns ``True`` only when the local view says leader AND the
        last backend response confirming leadership is at most
        ``max_age`` seconds old. Use this for fan-out work that must
        not run on a worker whose leadership is uncertain (for
        example, a global migration step or a single-writer task that
        is not separately fenced).

        Args:
            max_age: Maximum acceptable age in seconds since the
                last backend-confirmed renewal. Typical values are
                less than `renew_deadline`.
        """
        if not self._is_leader or self._last_confirmed_at is None:
            return False
        return (monotonic() - self._last_confirmed_at) <= max_age

    async def wait_for_leader(self) -> None:
        """Wait until the current worker is the leader."""
        while not self.is_leader():
            async with self._state_change_condition:
                await self._state_change_condition.wait()

    async def wait_lose_leader(self) -> None:
        """Wait until the current worker is no longer the leader."""
        while self.is_leader():
            config = self._config
            timeout = self._seconds_before_expiration_deadline(config)
            try:
                async with self._state_change_condition:
                    await asyncio.wait_for(
                        self._state_change_condition.wait(), timeout
                    )
            except TimeoutError:
                pass

    async def __call__(
        self,
        *,
        ready: asyncio.Future[None] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run polling loop service to acquire or renew the distributed lock."""
        if ready is not None and not ready.done():  # pragma: no branch
            ready.set_result(None)
        if self._service_running:
            logger.warning("Leader Election already running: %s", self.name)
            return
        self._service_running = True
        logger.info("Leader Election started: %s", self.name)
        try:
            while True:
                config = self._config
                await self._try_acquire_or_renew(config)
                # On a graceful stop, break and let the finally block
                # release leadership on the backend before unwinding.
                if await sleep_or_stop(config.retry_interval, stop):
                    break
        except asyncio.CancelledError:
            logger.info("Leader Election stopped: %s", self.name)
            raise
        except BaseException:
            logger.exception("Leader Election crashed: %s", self.name)
            raise
        finally:
            self._service_running = False
            # Run release as a separate task and keep waiting through
            # repeated cancellations so the lock is released on the
            # backend before the loop unwinds. asyncio.shield only
            # protects the inner task from the awaiter's cancel; on a
            # re-cancel of the awaiter, the inner task keeps running.
            release_task = asyncio.ensure_future(self._release())
            while not release_task.done():
                try:
                    await asyncio.shield(release_task)
                except asyncio.CancelledError:  # pragma: no cover
                    continue

    async def _update_state(
        self, *, is_leader: bool, reason_if_no_more_leader: str
    ) -> None:
        """Update the state of the leader election."""
        now = monotonic()
        self._state_updated_at = now
        if is_leader:
            self._last_confirmed_at = now
        else:
            self._last_confirmed_at = None
        if is_leader is self._is_leader:
            return  # No change

        self._is_leader = is_leader

        if is_leader:
            logger.info("Leader Election acquired leadership: %s", self.name)
        else:
            logger.warning(
                "Leader Election lost leadership: %s (%s)",
                self.name,
                reason_if_no_more_leader,
            )

        async with self._state_change_condition:
            self._state_change_condition.notify_all()

    async def _try_acquire_or_renew(self, config: LeaderElectionConfig) -> None:
        """Try to acquire leadership using `config` as the operation snapshot."""
        backend = self.backend
        try:
            async with asyncio.timeout(config.backend_timeout):
                record = await backend.acquire_or_renew(
                    name=self._lock_name,
                    token=str(config.worker),
                    duration=config.lease_duration,
                    metadata=self._metadata,
                )
        except Exception:
            if self._check_error_interval(config):
                logger.exception(
                    "Leader Election failed to acquire lock: %s", self.name
                )
            if self._is_renew_deadline_reached(config):
                await self._update_state(
                    is_leader=False,
                    reason_if_no_more_leader="renew deadline reached",
                )
        else:
            self._record = record
            await self._update_state(
                is_leader=record.holder == str(config.worker),
                reason_if_no_more_leader="lock not acquired",
            )

    def _seconds_before_expiration_deadline(
        self, config: LeaderElectionConfig
    ) -> float:
        return max(
            self._state_updated_at + config.lease_duration - monotonic(),
            0,
        )

    def _check_error_interval(self, config: LeaderElectionConfig) -> bool:
        """Check if the cooldown interval allows to log the error."""
        is_logging_allowed = (
            not self._error_logged_at
            or (monotonic() - self._error_logged_at) > config.error_interval
        )
        self._error_logged_at = monotonic()
        return is_logging_allowed

    def _is_renew_deadline_reached(self, config: LeaderElectionConfig) -> bool:
        return (monotonic() - self._state_updated_at) >= config.renew_deadline

    def guard(self) -> "_LeaderGuard":
        """Return a non-blocking synchronization guard.

        The guard raises ``WouldBlock`` if the current worker is not the leader,
        making it suitable for use as the ``sync`` parameter of ``IntervalTask``.

        Unlike using ``LeaderElection`` directly (which blocks until leader),
        the guard skips the current tick and retries on the next interval.
        """
        return _LeaderGuard(self)

    async def _apply_reconfigure(
        self, new_config: LeaderElectionConfig
    ) -> None:
        """Validate the immutable `worker` field before publishing `new_config`."""
        if new_config.worker != self._config.worker:
            msg = (
                f"reconfigure cannot change worker "
                f"({self._config.worker!r} -> {new_config.worker!r}). "
                f"Reuse the existing worker on the new config."
            )
            raise ValueError(msg)

    async def _release(self) -> None:
        backend = self._backend
        if backend is None:
            # Nothing was acquired, nothing to release.
            return
        config = self._config
        try:
            async with asyncio.timeout(config.backend_timeout):
                released = await backend.release(
                    name=self._lock_name, token=str(config.worker)
                )
            if not released:
                logger.info(
                    "Leader Election lock already released: %s", self.name
                )
        except Exception:
            logger.exception(
                "Leader Election failed to release lock: %s", self.name
            )


class _LeaderGuard(LockPrimitive):
    """Non-blocking leader election guard.

    Raises ``WouldBlock`` on entry if the worker is not the leader.
    """

    def __init__(self, election: LeaderElection) -> None:
        self._election = election

    async def __aenter__(self) -> Self:
        """Enter the guard, raising WouldBlockError if not leader."""
        if not self._election.is_leader():
            msg = f"Not leader: {self._election.name}"
            raise WouldBlockError(msg)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the guard (no-op)."""
        return None
