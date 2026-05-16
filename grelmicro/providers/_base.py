"""Base class for `Provider` implementations."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from grelmicro.cache._protocol import CacheBackend
    from grelmicro.resilience._protocol import (
        CircuitBreakerBackend,
        RateLimiterBackend,
    )
    from grelmicro.sync.abc import SyncBackend


class Provider(AbstractAsyncContextManager["Provider"]):
    """Base class for vendor connection providers.

    A `Provider` owns the native client (e.g. `redis.asyncio.Redis`,
    `asyncpg.Pool`) and the URL or credentials that built it. Components
    (`Sync`, `Cache`, `RateLimit`, ...) accept a `Provider` and ask it for
    the canonical adapter via the factory methods below.

    Subclasses implement any subset of the factory methods. Factories that
    do not apply raise `NotImplementedError` with a message pointing to the
    nearest viable Provider or Adapter.

    Attributes:
        short_name: Vendor identifier (e.g. `"redis"`, `"postgres"`). Used
            for vendor identification in error messages and introspection.
    """

    short_name: ClassVar[str]

    def sync(self, **kwargs: Any) -> SyncBackend:  # noqa: ANN401
        """Return the canonical `SyncBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a sync adapter.
        """
        msg = (
            f"{type(self).__name__} has no sync adapter. "
            f"Pass a SyncBackend instance to Sync(...) directly."
        )
        raise NotImplementedError(msg)

    def cache(self, **kwargs: Any) -> CacheBackend:  # noqa: ANN401
        """Return the canonical `CacheBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a cache adapter.
        """
        msg = (
            f"{type(self).__name__} has no cache adapter. "
            f"Pass a CacheBackend instance to Cache(...) directly."
        )
        raise NotImplementedError(msg)

    def ratelimiter(self, **kwargs: Any) -> RateLimiterBackend:  # noqa: ANN401
        """Return the canonical `RateLimiterBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a rate limiter
                adapter.
        """
        msg = (
            f"{type(self).__name__} has no rate limiter adapter. "
            f"Pass a RateLimiterBackend instance to RateLimit(...) directly."
        )
        raise NotImplementedError(msg)

    def breaker(self, **kwargs: Any) -> CircuitBreakerBackend:  # noqa: ANN401
        """Return the canonical `CircuitBreakerBackend` adapter for this Provider.

        Raises:
            NotImplementedError: If this Provider does not ship a circuit
                breaker adapter.
        """
        msg = (
            f"{type(self).__name__} has no circuit breaker adapter. "
            f"Pass a CircuitBreakerBackend instance to Breaker(...) directly."
        )
        raise NotImplementedError(msg)
