"""Coordination component for the Grelmicro app object."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    NamedTuple,
    Self,
    cast,
)

from typing_extensions import Doc

from grelmicro._backend_kinds import (
    BackendKind,
    most_specific_backend,
    resolve_source,
)
from grelmicro._component import instantiate_if_class
from grelmicro._environment import SUSPENDED, record_coordination
from grelmicro.coordination._protocol import (
    LeaderElectionBackend,
    LockBackend,
    ReadWriteLockBackend,
    ScheduleBackend,
)
from grelmicro.coordination.errors import CoordinationBackendError
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.lock import Lock
from grelmicro.coordination.readwritelock import ReadWriteLock
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from grelmicro.types import BackendScope


class CoordinationBackend(NamedTuple):
    """One of the backends a `Coordination` holds."""

    keyword: str
    """Keyword it is passed under, and the `_{keyword}_backend` attribute."""

    factory: str
    """`Provider` method that builds it."""

    protocol: type
    """Backend Protocol an instance of it satisfies."""


COORDINATION_BACKENDS: Final = (
    CoordinationBackend("lock", "lock_backend", LockBackend),
    CoordinationBackend(
        "readwritelock", "readwritelock_backend", ReadWriteLockBackend
    ),
    CoordinationBackend(
        "leaderelection", "leaderelection_backend", LeaderElectionBackend
    ),
    CoordinationBackend("schedule", "schedule_backend", ScheduleBackend),
)
"""Every backend a `Coordination` holds, in wiring order.

One list, read by everything that walks a `Coordination`: the component
itself, the app's Provider discovery, the wrapping of a bare backend into its
Component, and the backend scope check. A fifth backend added here reaches all
four.
"""


def _slot_serving(backend: object) -> CoordinationBackend:
    """Return the slot a coordination backend fills.

    Raises:
        AmbiguousBackendError: If it serves two slots and neither protocol
            subsumes the other.
    """
    matches = [
        BackendKind(slot.protocol, f"Coordination({slot.keyword}=...)", None)
        for slot in COORDINATION_BACKENDS
        if isinstance(backend, slot.protocol)
    ]
    protocol = most_specific_backend(matches, backend).protocol
    return next(
        slot for slot in COORDINATION_BACKENDS if slot.protocol is protocol
    )


class Coordination:
    """Coordination component: wraps backends and exposes coordination primitives.

    Registered as `micro.coordination` after `Grelmicro.use(Coordination(...))`.
    Exposes `lock(...)`, `tasklock(...)`, and `leaderelection(...)` so users do
    not need to pass `backend=` on every primitive.

    A single positional `Provider` resolves every primitive: the component calls
    `provider.lock_backend()` for the lock backend,
    `provider.leaderelection_backend()` for the election backend, and
    `provider.schedule_backend()` for the cron schedule backend.
    The `lock=`, `readwritelock=`, `leaderelection=`, and `schedule=` keywords
    set each backend independently, so locks can run on one vendor and leader
    election on another.
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

    Read more in the [Coordination](../coordination/index.md) docs.
    """

    kind: ClassVar[str] = "coordination"

    default_requires: ClassVar[BackendScope] = "cluster"
    """Default `requires=`: a lock, a leader and a cron fire hold fleet-wide."""

    def __init__(
        self,
        source: Annotated[
            Provider
            | LockBackend
            | ReadWriteLockBackend
            | LeaderElectionBackend
            | ScheduleBackend
            | type[
                Provider
                | LockBackend
                | ReadWriteLockBackend
                | LeaderElectionBackend
                | ScheduleBackend
            ]
            | None,
            Doc(
                """
                A `Provider` (e.g. `RedisProvider`) that resolves every
                primitive, or one coordination backend. The component calls
                `provider.lock_backend()` for the lock backend,
                `provider.leaderelection_backend()` for the election backend, and so
                on for each kind the Provider ships. A backend fills the one
                slot its kind serves, so `Coordination(MemoryLockAdapter())`
                is `Coordination(lock=MemoryLockAdapter())`. A zero-arg class
                is instantiated for you. Use the per-backend keywords to set
                any backend independently.
                """,
            ),
        ] = None,
        *,
        lock: Annotated[
            Provider | LockBackend | type[Provider | LockBackend] | None,
            Doc(
                """
                The lock backend. A `Provider` resolves it via
                `provider.lock_backend()`, a `LockBackend` instance is used directly,
                and a zero-arg class is instantiated for you. Overrides the
                lock backend resolved from `source`.
                """,
            ),
        ] = None,
        leaderelection: Annotated[
            Provider
            | LeaderElectionBackend
            | type[Provider | LeaderElectionBackend]
            | None,
            Doc(
                """
                The leader election backend. A `Provider` resolves it via
                `provider.leaderelection_backend()`, a `LeaderElectionBackend`
                instance is used directly, and a zero-arg class is
                instantiated for you. Overrides the election backend resolved
                from `source`.
                """,
            ),
        ] = None,
        readwritelock: Annotated[
            Provider
            | ReadWriteLockBackend
            | type[Provider | ReadWriteLockBackend]
            | None,
            Doc(
                """
                The read-write lock backend. A `Provider` resolves it via
                `provider.readwritelock_backend()`, a `ReadWriteLockBackend` instance
                is used directly, and a zero-arg class is instantiated for
                you. Overrides the read-write lock backend resolved from
                `source`.
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
                `provider.schedule_backend()`, a `ScheduleBackend` instance is used
                directly, and a zero-arg class is instantiated for you.
                Overrides the schedule backend resolved from `source`.
                """,
            ),
        ] = None,
        requires: Annotated[
            BackendScope | None,
            Doc(
                """
                The smallest backend scope this component accepts:
                `"process"`, `"host"` or `"cluster"`. Defaults to
                `"cluster"`, so a lock, a leader and a cron fire hold across
                every replica. Lower it to declare a single-process or
                single-host deployment. Checked when the app opens, see
                [the backend check](../deployment.md#the-backend-check).
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
        self._requires: BackendScope = requires or self.default_requires
        self._lock_backend: LockBackend | None = None
        self._readwritelock_backend: ReadWriteLockBackend | None = None
        self._leaderelection_backend: LeaderElectionBackend | None = None
        self._schedule_backend: ScheduleBackend | None = None

        if source is not None:
            resolved = resolve_source(
                source,
                owner="Coordination",
                expects="a coordination backend",
                protocols=[slot.protocol for slot in COORDINATION_BACKENDS],
            )
            if isinstance(resolved, Provider):
                self._fill_from_provider(resolved)
            else:
                slot = _slot_serving(resolved)
                setattr(self, f"_{slot.keyword}_backend", resolved)

        if lock is not None:
            resolved_lock = cast(
                "Provider | LockBackend",
                instantiate_if_class(lock),
            )
            self._lock_backend = (
                resolved_lock.lock_backend()
                if isinstance(resolved_lock, Provider)
                else resolved_lock
            )

        if readwritelock is not None:
            resolved_readwritelock = cast(
                "Provider | ReadWriteLockBackend",
                instantiate_if_class(readwritelock),
            )
            self._readwritelock_backend = (
                resolved_readwritelock.readwritelock_backend()
                if isinstance(resolved_readwritelock, Provider)
                else resolved_readwritelock
            )

        if leaderelection is not None:
            resolved_leaderelection = cast(
                "Provider | LeaderElectionBackend",
                instantiate_if_class(leaderelection),
            )
            self._leaderelection_backend = (
                resolved_leaderelection.leaderelection_backend()
                if isinstance(resolved_leaderelection, Provider)
                else resolved_leaderelection
            )

        if schedule is not None:
            resolved_schedule = cast(
                "Provider | ScheduleBackend",
                instantiate_if_class(schedule),
            )
            self._schedule_backend = (
                resolved_schedule.schedule_backend()
                if isinstance(resolved_schedule, Provider)
                else resolved_schedule
            )

    def _fill_from_provider(self, provider: Provider) -> None:
        """Fill every slot with the backend `provider` ships for it.

        A provider may not ship every adapter kind. Its `NotImplementedError`
        leaves that backend unset, so the kind raises a clear error only when
        it is actually used, instead of crashing construction for a
        locks-only user.
        """
        for slot in COORDINATION_BACKENDS:
            try:
                backend = getattr(provider, slot.factory)()
            except NotImplementedError:
                backend = None
            setattr(self, f"_{slot.keyword}_backend", backend)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def requires(self) -> BackendScope:
        """The smallest backend scope this component accepts."""
        return self._requires

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
    def readwritelock_backend(self) -> ReadWriteLockBackend:
        """The underlying `ReadWriteLockBackend`.

        Raises:
            CoordinationBackendError: If no read-write lock backend is wired.
        """
        if self._readwritelock_backend is None:
            msg = (
                "Coordination has no read-write lock backend. "
                "Pass a read-write lock provider as Coordination(provider) or "
                "Coordination(readwritelock=...)."
            )
            raise CoordinationBackendError(msg)
        return self._readwritelock_backend

    @property
    def leaderelection_backend(self) -> LeaderElectionBackend:
        """The underlying `LeaderElectionBackend`.

        Raises:
            CoordinationBackendError: If no leader election backend is wired.
        """
        if self._leaderelection_backend is None:
            msg = (
                "Coordination has no leader election backend. "
                "Pass an election provider as Coordination(provider) or "
                "Coordination(leaderelection=...)."
            )
            raise CoordinationBackendError(msg)
        return self._leaderelection_backend

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
        return self._build(Lock, name, self.lock_backend, "lock", kwargs)

    def tasklock(self, name: str, **kwargs: Any) -> TaskLock:  # noqa: ANN401
        """Construct a `TaskLock` bound to this component's lock backend.

        Raises:
            CoordinationBackendError: If no lock backend is wired.
        """
        return self._build(TaskLock, name, self.lock_backend, "lock", kwargs)

    def readwritelock(self, name: str, **kwargs: Any) -> ReadWriteLock:  # noqa: ANN401
        """Construct a `ReadWriteLock` bound to this component's backend.

        Raises:
            CoordinationBackendError: If no read-write lock backend is wired.
        """
        return self._build(
            ReadWriteLock,
            name,
            self.readwritelock_backend,
            "readwritelock",
            kwargs,
        )

    def leaderelection(
        self,
        name: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> LeaderElection:
        """Construct a `LeaderElection` bound to this component's election backend.

        Raises:
            CoordinationBackendError: If no leader election backend is wired.
        """
        return self._build(
            LeaderElection,
            name,
            self.leaderelection_backend,
            "leaderelection",
            kwargs,
        )

    def _build[P](
        self,
        pattern: Callable[..., P],
        name: str,
        backend: object,
        slot: str,
        kwargs: dict[str, Any],
    ) -> P:
        """Build a pattern on `backend`, checked against this `requires`.

        A registered component answers for its backend, so this costs one
        lookup. An unregistered one still has its declared reach honored.
        """
        # Set by hand rather than through `unrecorded()`: this runs on
        # every `micro.coordination.lock(...)`.
        token = SUSPENDED.set(True)
        try:
            built = pattern(name, backend=backend, **kwargs)
        finally:
            SUSPENDED.reset(token)
        record_coordination(built, backend, slot, self._requires)
        return built

    async def __aenter__(self) -> Self:
        """Open whichever backends are set."""
        if self._lock_backend is not None:
            await self._lock_backend.__aenter__()
        if self._readwritelock_backend is not None:
            await self._readwritelock_backend.__aenter__()
        if self._leaderelection_backend is not None:
            await self._leaderelection_backend.__aenter__()
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
                if self._readwritelock_backend is not None:
                    await self._readwritelock_backend.__aexit__(
                        exc_type, exc, tb
                    )
            finally:
                try:
                    if self._leaderelection_backend is not None:
                        await self._leaderelection_backend.__aexit__(
                            exc_type, exc, tb
                        )
                finally:
                    if self._schedule_backend is not None:
                        await self._schedule_backend.__aexit__(
                            exc_type, exc, tb
                        )
