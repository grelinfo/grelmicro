"""Leader Election."""

from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Self
from uuid import UUID

from anyio import (
    TASK_STATUS_IGNORED,
    CancelScope,
    Condition,
    WouldBlock,
    fail_after,
    get_cancelled_exc_class,
    move_on_after,
    sleep,
)
from anyio.abc import TaskStatus
from pydantic import model_validator
from typing_extensions import Doc

from grelmicro._config import env_segment, resolve_config
from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync._base import BaseLockConfig
from grelmicro.sync.abc import Seconds, SyncBackend, SyncPrimitive
from grelmicro.task.abc import Task

if TYPE_CHECKING:
    from contextlib import AsyncExitStack

    from anyio.abc import TaskGroup

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


class LeaderElection(SyncPrimitive, Task):
    """Leader Election.

    The leader election is a synchronization primitive with the worker as scope.
    It runs as a task to acquire or renew the distributed lock.
    """

    _LOCK_PREFIX = "leader"

    def __init__(
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
            SyncBackend | str | None,
            Doc(
                """
                The distributed lock backend used to acquire and release the lock.

                By default, it will use the lock backend registry to get the default lock backend.
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
                expired. When unset and env reads are enabled (see ``read_env`` and
                ``GREL_CONFIG_FROM_ENV``), resolves from the environment
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
        read_env: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_CONFIG_FROM_ENV`` flag. Pass True or False to
                override the flag for this construction.
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
            SyncBackend | str | None,
            Doc(
                """
                The distributed lock backend used to acquire and release the lock.

                By default, it will use the lock backend registry to get the default lock backend.
                """,
            ),
        ] = None,
    ) -> Self:
        """Construct a `LeaderElection` from a name and a pre-built `LeaderElectionConfig`."""
        instance = cls.__new__(cls)
        instance._setup(name, config, backend)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: LeaderElectionConfig,
        backend: SyncBackend | str | None,
    ) -> None:
        """Wire the validated config and runtime deps onto the instance."""
        self._name = name
        self._config = config
        self._lock_name = f"{self._LOCK_PREFIX}:{name}"
        self._backend: SyncBackend | None = (
            backend if not isinstance(backend, str) else None
        )
        self._backend_name: str | None = (
            backend if isinstance(backend, str) else None
        )

        self._service_running = False
        self._state_change_condition: Condition = Condition()
        self._is_leader: bool = False
        self._state_updated_at: float = monotonic()
        self._error_logged_at: float | None = None
        self._task_group: TaskGroup | None = None
        self._exit_stack: AsyncExitStack | None = None

    @property
    def config(self) -> LeaderElectionConfig:
        """Return the leader election config."""
        return self._config

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
    def backend(self) -> SyncBackend:
        """Bound sync backend, resolved on each call.

        When a backend instance was passed at construction it is
        always returned. Otherwise the registry is consulted on
        every access so that task-scoped ``sync.use(...)``
        overrides take effect.
        """
        if self._backend is not None:
            return self._backend
        return get_sync_backend(self._backend_name or "default")

    def is_running(self) -> bool:
        """Check if the leader election task is running."""
        return self._service_running

    def is_leader(self) -> bool:
        """Check if the current worker is the leader.

        To avoid a split-brain scenario, the leader considers itself as no longer leader if the
        renew deadline is reached.

        Returns:
            True if the current worker is the leader, False otherwise.

        """
        if not self._is_leader:
            return False
        return not self._is_renew_deadline_reached()

    async def wait_for_leader(self) -> None:
        """Wait until the current worker is the leader."""
        while not self.is_leader():
            async with self._state_change_condition:
                await self._state_change_condition.wait()

    async def wait_lose_leader(self) -> None:
        """Wait until the current worker is no longer the leader."""
        while self.is_leader():
            with move_on_after(self._seconds_before_expiration_deadline()):
                async with self._state_change_condition:
                    await self._state_change_condition.wait()

    async def __call__(
        self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED
    ) -> None:
        """Run polling loop service to acquire or renew the distributed lock."""
        task_status.started()
        if self._service_running:
            logger.warning("Leader Election already running: %s", self.name)
            return
        self._service_running = True
        logger.info("Leader Election started: %s", self.name)
        try:
            while True:
                await self._try_acquire_or_renew()
                await sleep(self._config.retry_interval)
        except get_cancelled_exc_class():
            logger.info("Leader Election stopped: %s", self.name)
            raise
        except BaseException:
            logger.exception("Leader Election crashed: %s", self.name)
            raise
        finally:
            self._service_running = False
            with CancelScope(shield=True):
                await self._release()

    async def _update_state(
        self, *, is_leader: bool, reason_if_no_more_leader: str
    ) -> None:
        """Update the state of the leader election."""
        self._state_updated_at = monotonic()
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

    async def _try_acquire_or_renew(self) -> None:
        """Try to acquire leadership."""
        backend = self.backend
        try:
            with fail_after(self._config.backend_timeout):
                is_leader = await backend.acquire(
                    name=self._lock_name,
                    token=str(self._config.worker),
                    duration=self._config.lease_duration,
                )
        except Exception:
            if self._check_error_interval():
                logger.exception(
                    "Leader Election failed to acquire lock: %s", self.name
                )
            if self._is_renew_deadline_reached():
                await self._update_state(
                    is_leader=False,
                    reason_if_no_more_leader="renew deadline reached",
                )
        else:
            await self._update_state(
                is_leader=is_leader,
                reason_if_no_more_leader="lock not acquired",
            )

    def _seconds_before_expiration_deadline(self) -> float:
        return max(
            self._state_updated_at + self._config.lease_duration - monotonic(),
            0,
        )

    def _check_error_interval(self) -> bool:
        """Check if the cooldown interval allows to log the error."""
        is_logging_allowed = (
            not self._error_logged_at
            or (monotonic() - self._error_logged_at)
            > self._config.error_interval
        )
        self._error_logged_at = monotonic()
        return is_logging_allowed

    def _is_renew_deadline_reached(self) -> bool:
        return (
            monotonic() - self._state_updated_at
        ) >= self._config.renew_deadline

    def guard(self) -> "_LeaderGuard":
        """Return a non-blocking synchronization guard.

        The guard raises ``WouldBlock`` if the current worker is not the leader,
        making it suitable for use as the ``sync`` parameter of ``IntervalTask``.

        Unlike using ``LeaderElection`` directly (which blocks until leader),
        the guard skips the current tick and retries on the next interval.
        """
        return _LeaderGuard(self)

    async def _release(self) -> None:
        backend = self._backend
        if backend is None:
            # Nothing was acquired, nothing to release.
            return
        try:
            with fail_after(self._config.backend_timeout):
                if not (
                    await backend.release(
                        name=self._lock_name, token=str(self._config.worker)
                    )
                ):
                    logger.info(
                        "Leader Election lock already released: %s", self.name
                    )
        except Exception:
            logger.exception(
                "Leader Election failed to release lock: %s", self.name
            )


class _LeaderGuard(SyncPrimitive):
    """Non-blocking leader election guard.

    Raises ``WouldBlock`` on entry if the worker is not the leader.
    """

    def __init__(self, election: LeaderElection) -> None:
        self._election = election

    async def __aenter__(self) -> Self:
        """Enter the guard, raising WouldBlock if not leader."""
        if not self._election.is_leader():
            msg = f"Not leader: {self._election.name}"
            raise WouldBlock(msg)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the guard (no-op)."""
        return None
