"""Sync component for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro.providers._base import Provider
from grelmicro.sync.lock import Lock
from grelmicro.sync.tasklock import TaskLock

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.sync.abc import SyncBackend


class Sync:
    """Sync component: wraps a `SyncBackend` and exposes lock primitives.

    Registered as `micro.sync` after `Grelmicro.use(Sync(...))`. Exposes
    `lock(...)` and `task_lock(...)` factories so users do not need to pass
    `backend=` on every primitive. Leader election lives on the separate
    `Coordination` component so it can run on a different backend.

    Accepts a `Provider` or a `SyncBackend`. When given a Provider, the
    component calls `provider.sync()` to build the matching adapter.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.providers.redis import RedisProvider
        from grelmicro.sync import Sync

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(uses=[redis, Sync(redis)])

        async with micro:
            async with micro.sync.lock("cart"):
                ...
        ```

    Read more in the [Synchronization](../sync.md) docs.
    """

    kind: ClassVar[str] = "sync"

    def __init__(
        self,
        source: Annotated[
            Provider | SyncBackend | type[Provider | SyncBackend],
            Doc(
                """
                A `Provider` (e.g. `RedisProvider`) or a `SyncBackend`
                instance. When a Provider is given, the component calls
                `provider.sync()` to build the matching adapter. A zero-arg
                class (e.g. `MemorySyncAdapter`) is instantiated for you.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Sync` components may coexist on
                one `Grelmicro` under different names.
                """,
            ),
        ] = "default",
    ) -> None:
        """Initialize the component with the wrapped backend."""
        self.name = name
        source = instantiate_if_class(source)
        if isinstance(source, Provider):
            self._backend = source.sync()
        else:
            self._backend = source

    @property
    def backend(self) -> SyncBackend:
        """The underlying `SyncBackend`."""
        return self._backend

    def lock(self, name: str, **kwargs: Any) -> Lock:  # noqa: ANN401
        """Construct a `Lock` bound to this component's backend."""
        return Lock(name, backend=self._backend, **kwargs)

    def task_lock(self, name: str, **kwargs: Any) -> TaskLock:  # noqa: ANN401
        """Construct a `TaskLock` bound to this component's backend."""
        return TaskLock(name, backend=self._backend, **kwargs)

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
