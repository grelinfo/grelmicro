"""Leader Election."""

from logging import getLogger
from time import monotonic
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Self
from uuid import UUID, uuid1

from anyio import (
    TASK_STATUS_IGNORED,
    CancelScope,
    Condition,
    fail_after,
    get_cancelled_exc_class,
    move_on_after,
    sleep,
)
from anyio.abc import TaskStatus
from pydantic import BaseModel, model_validator
from typing_extensions import Doc

from grelmicro.sync._backends import get_sync_backend
from grelmicro.sync.abc import Seconds, SyncBackend, Synchronization
from grelmicro.task.abc import Task

if TYPE_CHECKING:
    from contextlib import AsyncExitStack

    from anyio.abc import TaskGroup

logger = getLogger("grelmicro.leader_election")


class LeaderElectionConfig(BaseModel):
    """Leader Election Config.

    Leader election based on a leased reentrant distributed lock.
    """

    name: Annotated[
        str,
        Doc(
            """
            The leader election lock name.
            """,
        ),
    ]
    worker: Annotated[
        str | UUID,
        Doc(
            """
            The worker identity used as lock token.
            """,
        ),
    ]
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


class LeaderElection(Synchronization, Task):
    """Leader Election.

    The leader election is a synchronization primitive with the worker as scope.
    It runs as a task to acquire or renew the distributed lock.
    """

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
            SyncBackend | None,
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

                By default, use a UUIDv1 will be generated.
                """,
            ),
        ] = None,
        lease_duration: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds after the lock will be released if not renewed.

                If the worker becomes unavailable, the lock can only be acquired by an other worker
                after it' has expired.
                """,
            ),
        ] = 15,
        renew_deadline: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds that the leader worker will try to acquire the lock before
                giving up.

                Must be shorter than the lease duration. In case of multiple failures, the leader
                worker will loose the lead to prevent split-brain scenarios and ensure that only one
                worker is the leader at any time.
                """,
            ),
        ] = 10,
        retry_interval: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds between attempts to acquire or renew the lock.

                Must be shorter than the renew deadline. A shorter schedule enables faster leader
                elections but may increase load on the distributed lock backend, while a longer
                schedule reduces load but can delay new leader elections.
                """,
            ),
        ] = 2,
        backend_timeout: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds for waiting on backend for acquiring and releasing the lock.

                This value determines how long the system will wait before giving up the current
                operation.
                """,
            ),
        ] = 5,
        error_interval: Annotated[
            Seconds,
            Doc(
                """
                The duration in seconds between logging error messages.

                If shorter than the retry interval, it will log every error. It is used to prevent
                flooding the logs when the lock backend is unavailable.
                """,
            ),
        ] = 30,
    ) -> None:
        """Initialize the leader election."""
        self.config = LeaderElectionConfig(
            name=name,
            worker=worker or uuid1(),
            lease_duration=lease_duration,
            renew_deadline=renew_deadline,
            retry_interval=retry_interval,
            backend_timeout=backend_timeout,
            error_interval=error_interval,
        )
        self.backend = backend or get_sync_backend()

        self._service_running = False
        self._state_change_condition: Condition = Condition()
        self._is_leader: bool = False
        self._state_updated_at: float = monotonic()
        self._error_logged_at: float | None = None
        self._task_group: TaskGroup | None = None
        self._exit_stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        """Wait for the leader with the context manager."""
        await self.wait_for_leader()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit the context manager."""

    @property
    def name(self) -> str:
        """Return the task name."""
        return self.config.name

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
                await sleep(self.config.retry_interval)
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
        self, *, is_leader: bool, raison_if_no_more_leader: str
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
                raison_if_no_more_leader,
            )

        async with self._state_change_condition:
            self._state_change_condition.notify_all()

    async def _try_acquire_or_renew(self) -> None:
        """Try to acquire leadership."""
        try:
            with fail_after(self.config.backend_timeout):
                is_leader = await self.backend.acquire(
                    name=self.name,
                    token=str(self.config.worker),
                    duration=self.config.lease_duration,
                )
        except Exception:
            if self._check_error_interval():
                logger.exception(
                    "Leader Election failed to acquire lock: %s", self.name
                )
            if self._is_renew_deadline_reached():
                await self._update_state(
                    is_leader=False,
                    raison_if_no_more_leader="renew deadline reached",
                )
        else:
            await self._update_state(
                is_leader=is_leader,
                raison_if_no_more_leader="lock not acquired",
            )

    def _seconds_before_expiration_deadline(self) -> float:
        return max(
            self._state_updated_at + self.config.lease_duration - monotonic(), 0
        )

    def _check_error_interval(self) -> bool:
        """Check if the cooldown interval allows to log the error."""
        is_logging_allowed = (
            not self._error_logged_at
            or (monotonic() - self._error_logged_at)
            > self.config.error_interval
        )
        self._error_logged_at = monotonic()
        return is_logging_allowed

    def _is_renew_deadline_reached(self) -> bool:
        return (
            monotonic() - self._state_updated_at
        ) >= self.config.renew_deadline

    async def _release(self) -> None:
        try:
            with fail_after(self.config.backend_timeout):
                if not (
                    await self.backend.release(
                        name=self.config.name, token=str(self.config.worker)
                    )
                ):
                    logger.info(
                        "Leader Election lock already released: %s", self.name
                    )
        except Exception:
            logger.exception(
                "Leader Election failed to release lock: %s", self.name
            )
