"""Providers: vendor-specific connection objects shared across components.

A `Provider` owns a native client (Redis pool, asyncpg pool, ...) and the
URL/credentials that built it. Components (`Coordination`, `Cache`, rate limiters,
...) accept a `Provider` instead of opening their own pools, so two
components against the same vendor share one connection.

Read more in the [Providers](providers.md) docs.
"""

from grelmicro.providers._base import Provider

__all__ = ["Provider"]
