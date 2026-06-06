"""Coordination component for the Grelmicro app object."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.coordination.abc import LeaderElectionBackend


class Coordination:
    """Coordination component: wraps a backend and exposes coordination primitives.

    Registered as `micro.coordination` after `Grelmicro.use(Coordination(...))`.
    Exposes `leader_election(...)` so users do not need to pass `backend=` on
    every primitive.

    A `Coordination` component is separate from `Sync`, so leader election can
    run on a different vendor than `Lock`. A service can keep `Lock` on Redis
    for low-latency mutual exclusion and run `LeaderElection` on a Kubernetes
    Lease, native to the cluster.

    Accepts a `Provider` or a `SyncBackend`. When given a Provider, the
    component calls `provider.coordination()` to build the matching adapter.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.coordination import Coordination
        from grelmicro.providers.redis import RedisProvider
        from grelmicro.sync import Sync

        redis = RedisProvider("redis://localhost:6379/0")
        micro = Grelmicro(uses=[Sync(redis), Coordination(redis)])

        async with micro:
            leader = micro.coordination.leader_election("worker")
        ```

    Read more in the [Coordination](../coordination.md) docs.
    """

    kind: ClassVar[str] = "coordination"

    def __init__(
        self,
        source: Annotated[
            Provider
            | LeaderElectionBackend
            | type[Provider | LeaderElectionBackend],
            Doc(
                """
                A `Provider` (e.g. `RedisProvider`) or a
                `LeaderElectionBackend` instance. When a Provider is given, the
                component calls `provider.leader_election()` to build the
                matching backend. A zero-arg class (e.g.
                `MemoryLeaderElectionBackend`) is instantiated for you.
                """,
            ),
        ],
        *,
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
        """Initialize the component with the wrapped backend."""
        self.name = name
        source = instantiate_if_class(source)
        if isinstance(source, Provider):
            self._backend = source.leader_election()
        else:
            self._backend = source

    @property
    def backend(self) -> LeaderElectionBackend:
        """The underlying `LeaderElectionBackend`."""
        return self._backend

    def leader_election(
        self,
        name: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> LeaderElection:
        """Construct a `LeaderElection` bound to this component's backend."""
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
