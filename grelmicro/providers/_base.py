"""Base class for `Provider` implementations."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, ClassVar, Final

if TYPE_CHECKING:
    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.coordination._protocol import (
        LeaderElectionBackend,
        LockBackend,
        ReadWriteLockBackend,
        ScheduleBackend,
    )
    from grelmicro.health._types import HealthDetails
    from grelmicro.outbox._protocol import OutboxBackend
    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        RateLimiterBackend,
    )


PATTERNS: Final = (
    "lock",
    "readwritelock",
    "leaderelection",
    "schedule",
    "cache",
    "outbox",
    "ratelimiter",
    "circuitbreaker",
)
"""Every pattern a `Provider` builds a backend for, with `<pattern>_backend()`."""


class Provider(AbstractAsyncContextManager["Provider"]):
    """Base class for vendor connection providers.

    A `Provider` owns the native client (e.g. `redis.asyncio.Redis`,
    `asyncpg.Pool`) and the URL or credentials that built it. Components
    (`Coordination`, `Cache`, `RateLimiterComponent`, ...) accept a `Provider` and ask it for
    the matching adapter via the factory methods below.

    Subclasses implement any subset of the factory methods. Factories that
    do not apply raise `NotImplementedError` with a message pointing to the
    nearest viable Provider or Adapter.

    Attributes:
        short_name: Vendor identifier (e.g. `"redis"`, `"postgres"`). Used
            for vendor identification in error messages and introspection.
    """

    short_name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401
        """Refuse a factory named after its pattern without `_backend`.

        A subclass with a `lock()` method, its own or from a mixin, and no
        `lock_backend()` is refused. A method named after a pattern is fine
        once the subclass, or a Provider it extends, defines that pattern's
        `_backend()` factory. A plain attribute named after a pattern is
        not checked.

        Raises:
            TypeError: If the subclass has a `<pattern>()` method and no
                `<pattern>_backend()` overrides the base one.
        """
        super().__init_subclass__(**kwargs)
        for pattern in PATTERNS:
            factory = f"{pattern}_backend"
            if callable(getattr(cls, pattern, None)) and getattr(
                cls, factory
            ) is getattr(Provider, factory):
                msg = (
                    f"Rename {cls.__name__}.{pattern}() to {factory}(). "
                    f"Components build a {pattern} backend with "
                    f"provider.{factory}()."
                )
                raise TypeError(msg)

    _skips: int = 0
    """How many open apps `micro.fake()` left this Provider closed in.

    A count rather than a flag, so one faked app closing does not clear the
    mark another still holds.
    """

    def _is_open(self) -> bool:
        """Return whether something opened this Provider.

        Overridden by each Provider that can tell. The default answers
        `False`, so a Provider that cannot tell counts as closed.
        """
        return False

    def _left_closed(self) -> bool:
        """Return whether `micro.fake()` left this Provider closed.

        Only while nothing else opened it: a test that opens it itself, or a
        real app sharing it, gets a working client.
        """
        return self._skips > 0 and not self._is_open()

    def _refuse_if_left_closed(self) -> None:
        """Say why this Provider is closed and how to keep it, if it was faked.

        Read by `client`, so a test that reaches a Provider the fake never
        opened is told so, instead of connecting for real.

        Raises:
            OutOfContextError: If `micro.fake()` left this Provider closed.
        """
        if self._left_closed():
            from grelmicro.errors import OutOfContextError  # noqa: PLC0415

            name = type(self).__name__
            msg = (
                f"{name} was left closed, because micro.fake() replaced every "
                f"component that uses it. Pass it as micro.fake(keep=[...]) "
                f"to open the real one in this test."
            )
            raise OutOfContextError(msg)

    def lock_backend(self, **kwargs: Any) -> LockBackend:  # noqa: ANN401
        """Return the matching `LockBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a lock adapter.
        """
        msg = (
            f"{type(self).__name__} has no lock adapter. "
            f"Pass a LockBackend instance to Coordination(lock=...) directly."
        )
        raise NotImplementedError(msg)

    def readwritelock_backend(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> ReadWriteLockBackend:
        """Return the matching `ReadWriteLockBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a read-write
                lock adapter.
        """
        msg = (
            f"{type(self).__name__} has no read-write lock adapter. "
            "Pass a ReadWriteLockBackend instance to "
            "Coordination(readwritelock=...) directly."
        )
        raise NotImplementedError(msg)

    def leaderelection_backend(
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
            "Pass a LeaderElectionBackend instance to "
            "Coordination(leaderelection=...) directly."
        )
        raise NotImplementedError(msg)

    def schedule_backend(self, **kwargs: Any) -> ScheduleBackend:  # noqa: ANN401
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

    def cache_backend(self, **kwargs: Any) -> CacheBackend:  # noqa: ANN401
        """Return the matching `CacheBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a cache adapter.
        """
        msg = (
            f"{type(self).__name__} has no cache adapter. "
            f"Pass a CacheBackend instance to Cache(...) directly."
        )
        raise NotImplementedError(msg)

    def outbox_backend(self, **kwargs: Any) -> OutboxBackend:  # noqa: ANN401
        """Return the matching `OutboxBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship an outbox
                adapter.
        """
        msg = (
            f"{type(self).__name__} has no outbox adapter. "
            f"Pass an OutboxBackend instance to Outbox(...) directly."
        )
        raise NotImplementedError(msg)

    def ratelimiter_backend(self, **kwargs: Any) -> RateLimiterBackend:  # noqa: ANN401
        """Return the matching `RateLimiterBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a rate limiter
                adapter.
        """
        msg = (
            f"{type(self).__name__} has no rate limiter adapter. "
            f"Pass a RateLimiterBackend instance to RateLimiterComponent(...) directly."
        )
        raise NotImplementedError(msg)

    def circuitbreaker_backend(self, **kwargs: Any) -> CircuitBreakerBackend:  # noqa: ANN401
        """Return the matching `CircuitBreakerBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a circuit
                breaker adapter.
        """
        msg = (
            f"{type(self).__name__} has no circuit breaker adapter. "
            f"Pass a CircuitBreakerBackend instance to CircuitBreakerComponent(...) directly."
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
