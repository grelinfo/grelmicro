"""Coordination component for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro.coordination.errors import CoordinationBackendError
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.coordination.abc import (
        LeaderElectionBackend,
        LockBackend,
        ScheduleBackend,
    )


class Coordination:
    """Coordination component: wraps backends and exposes coordination primitives.

    Registered as `micro.coordination` after `Grelmicro.use(Coordination(...))`.
    Exposes `lock(...)`, `tasklock(...)`, and `leaderelection(...)` so users do
    not need to pass `backend=` on every primitive.

    A single positional `Provider` resolves every primitive: the component calls
    `provider.lock()` for the lock backend, `provider.leaderelection()` for the
    election backend, and `provider.schedule()` for the cron schedule backend.
    The `lock=`, `election=`, and `schedule=` keywords set each backend
    independently, so locks can run on one vendor and leader election on another.
    Each accepts a `Provider`, a backend instance, or a zero-arg class.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.coordination import Coordination
        from grelmicro.providers.redis import RedisProvider

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(uses=[redis, Coordination(redis)])

        async with micro:
            async with micro.coordination.lock("cart"):
                ...
            leader = micro.coordination.leaderelection("worker")
        ```

    Read more in the [Coordination](../coordination.md) docs.
    """

    kind: ClassVar[str] = "coordination"

    def __init__(
        self,
        source: Annotated[
            Provider | type[Provider] | None,
            Doc(
                """
                A `Provider` (e.g. `RedisProvider`) that resolves both
                primitives. The component calls `provider.lock()` for the lock
                backend and `provider.leaderelection()` for the election
                backend. A zero-arg Provider class is instantiated for you.
                Use `lock=`/`election=` to set either backend independently.
                """,
            ),
        ] = None,
        *,
        lock: Annotated[
            Provider | LockBackend | type[Provider | LockBackend] | None,
            Doc(
                """
                The lock backend. A `Provider` resolves it via
                `provider.lock()`, a `LockBackend` instance is used directly,
                and a zero-arg class is instantiated for you. Overrides the
                lock backend resolved from `source`.
                """,
            ),
        ] = None,
        election: Annotated[
            Provider
            | LeaderElectionBackend
            | type[Provider | LeaderElectionBackend]
            | None,
            Doc(
                """
                The leader election backend. A `Provider` resolves it via
                `provider.leaderelection()`, a `LeaderElectionBackend`
                instance is used directly, and a zero-arg class is
                instantiated for you. Overrides the election backend resolved
                from `source`.
                """,
            ),
        ] = None,
        schedule: Annotated[
            Provider
            | ScheduleBackend
            | type[Provider | ScheduleBackend]
            | None,
            Doc(
                """
                The cron schedule backend. A `Provider` resolves it via
                `provider.schedule()`, a `ScheduleBackend` instance is used
                directly, and a zero-arg class is instantiated for you.
                Overrides the schedule backend resolved from `source`.
                """,
            ),
        ] = None,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Coordination` components may
                coexist on one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the component with the wrapped backends."""
        self._name = name
        self._lock_backend: LockBackend | None = None
        self._election_backend: LeaderElectionBackend | None = None
        self._schedule_backend: ScheduleBackend | None = None

        if source is not None:
            provider = instantiate_if_class(source)
            # A provider may not ship every adapter kind. Leave a backend
            # unset so the kind raises a clear error only when it is actually
            # used, instead of crashing construction for a locks-only user.
            try:
                self._lock_backend = provider.lock()
            except (AttributeError, NotImplementedError):
                self._lock_backend = None
            try:
                self._election_backend = provider.leaderelection()
            except (AttributeError, NotImplementedError):
                self._election_backend = None
            try:
                self._schedule_backend = provider.schedule()
            except (AttributeError, NotImplementedError):
                self._schedule_backend = None

        if lock is not None:
            resolved_lock = instantiate_if_class(lock)
            self._lock_backend = (
                resolved_lock.lock()
                if isinstance(resolved_lock, Provider)
                else resolved_lock
            )

        if election is not None:
            resolved_election = instantiate_if_class(election)
            self._election_backend = (
                resolved_election.leaderelection()
                if isinstance(resolved_election, Provider)
                else resolved_election
            )

        if schedule is not None:
            resolved_schedule = instantiate_if_class(schedule)
            self._schedule_backend = (
                resolved_schedule.schedule()
                if isinstance(resolved_schedule, Provider)
                else resolved_schedule
            )

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def lock_backend(self) -> LockBackend:
        """The underlying `LockBackend`.

        Raises:
            CoordinationBackendError: If no lock backend is wired.
        """
        if self._lock_backend is None:
            msg = (
                "Coordination has no lock backend. "
                "Pass a lock provider as Coordination(provider) or "
                "Coordination(lock=...)."
            )
            raise CoordinationBackendError(msg)
        return self._lock_backend

    @property
    def election_backend(self) -> LeaderElectionBackend:
        """The underlying `LeaderElectionBackend`.

        Raises:
            CoordinationBackendError: If no leader election backend is wired.
        """
        if self._election_backend is None:
            msg = (
                "Coordination has no leader election backend. "
                "Pass an election provider as Coordination(provider) or "
                "Coordination(election=...)."
            )
            raise CoordinationBackendError(msg)
        return self._election_backend

    @property
    def schedule_backend(self) -> ScheduleBackend:
        """The underlying `ScheduleBackend`.

        Raises:
            CoordinationBackendError: If no schedule backend is wired.
        """
        if self._schedule_backend is None:
            msg = (
                "Coordination has no schedule backend. "
                "Pass a schedule provider as Coordination(provider) or "
                "Coordination(schedule=...)."
            )
            raise CoordinationBackendError(msg)
        return self._schedule_backend

    def lock(self, name: str, **kwargs: Any) -> Lock:  # noqa: ANN401
        """Construct a `Lock` bound to this component's lock backend.

        Raises:
            CoordinationBackendError: If no lock backend is wired.
        """
        return Lock(name, backend=self.lock_backend, **kwargs)

    def tasklock(self, name: str, **kwargs: Any) -> TaskLock:  # noqa: ANN401
        """Construct a `TaskLock` bound to this component's lock backend.

        Raises:
            CoordinationBackendError: If no lock backend is wired.
        """
        return TaskLock(name, backend=self.lock_backend, **kwargs)

    def leaderelection(
        self,
        name: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> LeaderElection:
        """Construct a `LeaderElection` bound to this component's election backend.

        Raises:
            CoordinationBackendError: If no leader election backend is wired.
        """
        return LeaderElection(name, backend=self.election_backend, **kwargs)

    async def __aenter__(self) -> Self:
        """Open whichever backends are set."""
        if self._lock_backend is not None:
            await self._lock_backend.__aenter__()
        if self._election_backend is not None:
            await self._election_backend.__aenter__()
        if self._schedule_backend is not None:
            await self._schedule_backend.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close whichever backends are set, closing all even if one raises."""
        try:
            if self._lock_backend is not None:
                await self._lock_backend.__aexit__(exc_type, exc, tb)
        finally:
            try:
                if self._election_backend is not None:
                    await self._election_backend.__aexit__(exc_type, exc, tb)
            finally:
                if self._schedule_backend is not None:
                    await self._schedule_backend.__aexit__(exc_type, exc, tb)
