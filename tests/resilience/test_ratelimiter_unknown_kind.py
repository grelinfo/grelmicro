"""Every rate limiter backend rejects an unknown algorithm kind the same way.

The config union grows over time (`failure_rate` and `slow_call` are planned),
so a backend that predates a new arm must fail with the error the
`RateLimiterBackend.bind` protocol documents, never a silent default. This
mirrors the circuit breaker backends, which already contract on
`NotImplementedError`.

`bind` dispatches on the kind before it touches the provider client, so an
unsupported kind is rejected without a live connection.
"""

import pytest

from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience._protocol import RateLimiterBackend
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.resilience.ratelimiter.postgres import (
    PostgresRateLimiterAdapter,
)
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter
from grelmicro.resilience.ratelimiter.sqlite import SQLiteRateLimiterAdapter


class Fake:
    """An algorithm config kind no shipped backend knows about."""

    kind = "failure_rate"


BACKENDS = [
    pytest.param(MemoryRateLimiterAdapter(), id="memory"),
    pytest.param(
        RedisRateLimiterAdapter(
            provider=RedisProvider("redis://localhost:6379/0")
        ),
        id="redis",
    ),
    pytest.param(
        PostgresRateLimiterAdapter(
            provider=PostgresProvider(
                "postgresql://user:password@localhost:5432/db"
            )
        ),
        id="postgres",
    ),
    pytest.param(
        SQLiteRateLimiterAdapter(provider=SQLiteProvider(":memory:")),
        id="sqlite",
    ),
]


class HostileProvider:
    """A provider that fails on any attribute the adapter reaches for.

    `bind` must decide on the config kind before it touches the client, so
    rejecting an unknown kind never needs a live connection.
    """

    def __getattr__(self, name: str) -> object:
        """Fail loudly naming the attribute `bind` reached for."""
        msg = f"bind touched provider.{name} before checking the kind"
        raise AssertionError(msg)


@pytest.mark.parametrize("backend", BACKENDS)
def test_bind_rejects_unknown_kind(backend: RateLimiterBackend) -> None:
    """`bind` raises `NotImplementedError` naming the unsupported kind."""
    with pytest.raises(NotImplementedError, match="failure_rate"):
        backend.bind(Fake())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("backend", BACKENDS)
def test_bind_checks_the_kind_before_touching_the_client(
    backend: RateLimiterBackend,
) -> None:
    """`bind` rejects an unknown kind without reading the provider."""
    if hasattr(backend, "_provider"):
        backend._provider = HostileProvider()  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    with pytest.raises(NotImplementedError, match="failure_rate"):
        backend.bind(Fake())  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
