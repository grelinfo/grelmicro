"""Memory Provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self

from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.cache.memory import MemoryCacheAdapter
    from grelmicro.coordination.memory import (
        MemoryLeaderElectionAdapter,
        MemoryLockAdapter,
        MemoryScheduleAdapter,
    )
    from grelmicro.resilience.circuitbreaker.memory import (
        MemoryCircuitBreakerAdapter,
    )
    from grelmicro.resilience.ratelimiter.memory import (
        MemoryRateLimiterAdapter,
    )


class MemoryProvider(Provider):
    """In-process provider.

    Gives Memory the same provider-direct surface as Redis, Postgres, and
    SQLite. Each factory hands back one cached adapter per kind, so the
    provider owns a single in-process store per kind instead of handing out
    disconnected islands. `memory.lock()` called twice returns the same
    `MemoryLockAdapter`, so a later call re-fetches the live store for a test
    or an introspection. Wire each kind into one component, the same way you
    would a Redis adapter. The provider owns no external resource: it stores
    state in process and disappears on restart.

    Use it for tests and single-process apps. Reach for a Redis, Postgres,
    or SQLite provider for durable, distributed coordination.

    ```python
    from grelmicro import Grelmicro
    from grelmicro.coordination import Coordination
    from grelmicro.providers.memory import MemoryProvider

    memory = MemoryProvider()
    micro = Grelmicro(uses=[memory, Coordination(memory)])
    ```

    Read more in the [Providers](../providers.md) docs.
    """

    short_name: ClassVar[str] = "memory"

    def __init__(self) -> None:
        """Initialize the provider with empty per-kind adapter stores."""
        self._lock: MemoryLockAdapter | None = None
        self._leaderelection: MemoryLeaderElectionAdapter | None = None
        self._schedule: MemoryScheduleAdapter | None = None
        self._cache: MemoryCacheAdapter | None = None
        self._ratelimiter: MemoryRateLimiterAdapter | None = None
        self._circuitbreaker: MemoryCircuitBreakerAdapter | None = None

    def __repr__(self) -> str:
        """Return a representation of the provider."""
        return "MemoryProvider()"

    def lock(self, **kwargs: Any) -> MemoryLockAdapter:  # noqa: ANN401
        """Return the shared `MemoryLockAdapter` for this provider."""
        if self._lock is None:
            from grelmicro.coordination.memory import (  # noqa: PLC0415
                MemoryLockAdapter,
            )

            self._lock = MemoryLockAdapter(**kwargs)
        return self._lock

    def leaderelection(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> MemoryLeaderElectionAdapter:
        """Return the shared `MemoryLeaderElectionAdapter` for this provider."""
        if self._leaderelection is None:
            from grelmicro.coordination.memory import (  # noqa: PLC0415
                MemoryLeaderElectionAdapter,
            )

            self._leaderelection = MemoryLeaderElectionAdapter(**kwargs)
        return self._leaderelection

    def schedule(self, **kwargs: Any) -> MemoryScheduleAdapter:  # noqa: ANN401
        """Return the shared `MemoryScheduleAdapter` for this provider."""
        if self._schedule is None:
            from grelmicro.coordination.memory import (  # noqa: PLC0415
                MemoryScheduleAdapter,
            )

            self._schedule = MemoryScheduleAdapter(**kwargs)
        return self._schedule

    def cache(self, **kwargs: Any) -> MemoryCacheAdapter:  # noqa: ANN401
        """Return the shared `MemoryCacheAdapter` for this provider."""
        if self._cache is None:
            from grelmicro.cache.memory import (  # noqa: PLC0415
                MemoryCacheAdapter,
            )

            self._cache = MemoryCacheAdapter(**kwargs)
        return self._cache

    def ratelimiter(self, **kwargs: Any) -> MemoryRateLimiterAdapter:  # noqa: ANN401
        """Return the shared `MemoryRateLimiterAdapter` for this provider."""
        if self._ratelimiter is None:
            from grelmicro.resilience.ratelimiter.memory import (  # noqa: PLC0415
                MemoryRateLimiterAdapter,
            )

            self._ratelimiter = MemoryRateLimiterAdapter(**kwargs)
        return self._ratelimiter

    def circuitbreaker(self, **kwargs: Any) -> MemoryCircuitBreakerAdapter:  # noqa: ANN401
        """Return the shared `MemoryCircuitBreakerAdapter` for this provider."""
        if self._circuitbreaker is None:
            from grelmicro.resilience.circuitbreaker.memory import (  # noqa: PLC0415
                MemoryCircuitBreakerAdapter,
            )

            self._circuitbreaker = MemoryCircuitBreakerAdapter(**kwargs)
        return self._circuitbreaker

    async def check(self) -> None:
        """Report readiness. The in-process backend is always ready."""

    async def __aenter__(self) -> Self:
        """Open the provider. It owns no external resource."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider. It owns no external resource."""
