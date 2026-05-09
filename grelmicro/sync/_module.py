"""Sync module for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.sync.leaderelection import LeaderElection
from grelmicro.sync.lock import Lock
from grelmicro.sync.tasklock import TaskLock

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.sync.abc import SyncBackend


class Sync:
    """Sync module: wraps a `SyncBackend` and exposes lock primitives.

    Registered as `micro.sync` after `Grelmicro.use(Sync(backend))`. Exposes
    `lock(...)`, `task_lock(...)`, and `leader_election(...)` factories so
    users do not need to pass `backend=` on every primitive.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.sync import Sync
        from grelmicro.sync.redis import RedisSyncAdapter

        micro = Grelmicro(uses=[Sync(RedisSyncAdapter("redis://localhost"))])

        async with micro:
            async with micro.sync.lock("cart"):
                ...
        ```

    Read more in the [Synchronization](../sync.md) docs.
    """

    kind: ClassVar[str] = "sync"

    def __init__(
        self,
        backend: Annotated[
            SyncBackend,
            Doc("The synchronization backend opened with the module."),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Sync` modules may coexist on one
                `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the module with the wrapped backend."""
        self.name = name
        self._backend = backend

    @property
    def backend(self) -> SyncBackend:
        """The underlying `SyncBackend`."""
        return self._backend

    def lock(self, name: str, **kwargs: Any) -> Lock:  # noqa: ANN401
        """Construct a `Lock` bound to this module's backend."""
        return Lock(name, backend=self._backend, **kwargs)

    def task_lock(self, name: str, **kwargs: Any) -> TaskLock:  # noqa: ANN401
        """Construct a `TaskLock` bound to this module's backend."""
        return TaskLock(name, backend=self._backend, **kwargs)

    def leader_election(
        self,
        name: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> LeaderElection:
        """Construct a `LeaderElection` bound to this module's backend."""
        return LeaderElection(name, backend=self._backend, **kwargs)

    async def __aenter__(self) -> Self:
        """Open the underlying backend."""
        await self._backend.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the underlying backend."""
        return await self._backend.__aexit__(exc_type, exc, tb)
