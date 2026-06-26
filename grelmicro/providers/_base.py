"""Base class for `Provider` implementations."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.coordination._protocol import (
        LeaderElectionBackend,
        LockBackend,
        ScheduleBackend,
    )
    from grelmicro.health._types import HealthDetails
    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        RateLimiterBackend,
    )


class Provider(AbstractAsyncContextManager["Provider"]):
    """Base class for vendor connection providers.

    A `Provider` owns the native client (e.g. `redis.asyncio.Redis`,
    `asyncpg.Pool`) and the URL or credentials that built it. Components
    (`Coordination`, `Cache`, `RateLimiterRegistry`, ...) accept a `Provider` and ask it for
    the matching adapter via the factory methods below.

    Subclasses implement any subset of the factory methods. Factories that
    do not apply raise `NotImplementedError` with a message pointing to the
    nearest viable Provider or Adapter.

    Attributes:
        short_name: Vendor identifier (e.g. `"redis"`, `"postgres"`). Used
            for vendor identification in error messages and introspection.
    """

    short_name: ClassVar[str]

    def lock(self, **kwargs: Any) -> LockBackend:  # noqa: ANN401
        """Return the matching `LockBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a lock adapter.
        """
        msg = (
            f"{type(self).__name__} has no lock adapter. "
            f"Pass a LockBackend instance to Coordination(lock=...) directly."
        )
        raise NotImplementedError(msg)

    def leaderelection(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> LeaderElectionBackend:
        """Return the matching `LeaderElectionBackend` for this Provider.

        Leader election stores a `LeaderRecord` (holder, lease times, metadata),
        so it needs a backend that can hold that record, not a plain lock.

        Raises:
            NotImplementedError: If this Provider does not ship a leader
                election adapter.
        """
        msg = (
            f"{type(self).__name__} has no leader election adapter. "
            f"Pass a LeaderElectionBackend instance to Coordination(...) "
            f"directly."
        )
        raise NotImplementedError(msg)

    def schedule(self, **kwargs: Any) -> ScheduleBackend:  # noqa: ANN401
        """Return the matching `ScheduleBackend` adapter for this Provider.

        The schedule backend holds the durable `last_fired` state behind
        distributed cron.

        Raises:
            NotImplementedError: If this Provider does not ship a schedule
                adapter.
        """
        msg = (
            f"{type(self).__name__} has no schedule adapter. "
            f"Pass a ScheduleBackend instance to Coordination(schedule=...) "
            f"directly."
        )
        raise NotImplementedError(msg)

    def cache(self, **kwargs: Any) -> CacheBackend:  # noqa: ANN401
        """Return the matching `CacheBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a cache adapter.
        """
        msg = (
            f"{type(self).__name__} has no cache adapter. "
            f"Pass a CacheBackend instance to Cache(...) directly."
        )
        raise NotImplementedError(msg)

    def ratelimiter(self, **kwargs: Any) -> RateLimiterBackend:  # noqa: ANN401
        """Return the matching `RateLimiterBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a rate limiter
                adapter.
        """
        msg = (
            f"{type(self).__name__} has no rate limiter adapter. "
            f"Pass a RateLimiterBackend instance to RateLimiterRegistry(...) directly."
        )
        raise NotImplementedError(msg)

    def circuitbreaker(self, **kwargs: Any) -> CircuitBreakerBackend:  # noqa: ANN401
        """Return the matching `CircuitBreakerBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a circuit
                breaker adapter.
        """
        msg = (
            f"{type(self).__name__} has no circuit breaker adapter. "
            f"Pass a CircuitBreakerBackend instance to CircuitBreakerRegistry(...) directly."
        )
        raise NotImplementedError(msg)

    async def check(self) -> HealthDetails | None:
        """Run a cheap readiness probe against the backend.

        A `HealthChecks` registers this as a `provider:{short_name}` check
        via `add_provider` or `auto_health`. Returns `None` on success.
        Raises on failure: the exception surfaces in the health report and
        flips the check to `error`.

        Raises:
            NotImplementedError: If this Provider has no backend to probe.
        """
        msg = (
            f"{type(self).__name__} has no readiness check. "
            f"Register a custom check with health.check(...) instead."
        )
        raise NotImplementedError(msg)

    def instrument(self, tracer_provider: Any) -> bool:  # noqa: ANN401, ARG002
        """Attach OpenTelemetry instrumentation for this Provider's client.

        Called by `Trace(instrument=...)` after the app is open, with the
        app's `TracerProvider`. Returns whether instrumentation is in effect.
        The default returns `True`: a Provider with no native client to trace
        (such as Memory) has nothing to attach and that is not a failure.
        Subclasses override to attach the matching instrumentor, preferring
        per-instance attachment, and return `False` when the instrumentor
        package is absent so a named-but-uninstrumented target can warn.
        """
        return True

    def uninstrument(self) -> None:
        """Reverse `instrument`. The default is a no-op."""
