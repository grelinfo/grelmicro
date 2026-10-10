"""Each cache backend stores a TTL rounded up to its own resolution."""

from datetime import timedelta
from fractions import Fraction
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from grelmicro._duration import MAX_DURATION, MICROSECOND
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.postgres import PostgresCacheAdapter
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.cache.sqlite import _expires_at
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider

pytestmark = [pytest.mark.timeout(5)]

NOW_NS = 1_700_000_000_123_456_789
"""A wall-clock time in nanoseconds, with digits below the microsecond."""

REDIS_ROUNDING = [
    pytest.param(timedelta(microseconds=1), "1", id="one-microsecond"),
    pytest.param(timedelta(microseconds=999), "1", id="under-a-millisecond"),
    pytest.param(timedelta(milliseconds=1), "1", id="one-millisecond"),
    pytest.param(timedelta(microseconds=1001), "2", id="partial-millisecond"),
    pytest.param(timedelta(milliseconds=500), "500", id="half-second"),
    pytest.param(timedelta(seconds=30), "30000", id="whole-seconds"),
]


def _redis() -> tuple[RedisCacheAdapter, MagicMock]:
    provider = RedisProvider("redis://localhost:6379/0")
    client = MagicMock()
    client.eval = AsyncMock()
    provider._client = client
    return RedisCacheAdapter(provider=provider), client


@pytest.mark.parametrize(("ttl", "px"), REDIS_ROUNDING)
async def test_redis_cache_set_rounds_ttl_up_to_the_millisecond(
    ttl: timedelta, px: str
) -> None:
    """`set` stores the TTL in whole milliseconds, rounded up, never zero."""
    # Arrange
    backend, client = _redis()

    # Act
    await backend.set(key="k", value=b"v", ttl=ttl)

    # Assert
    client.eval.assert_awaited_once_with(ANY, 2, "k", "cache:rtag:k", b"v", px)


@pytest.mark.parametrize(("ttl", "px"), REDIS_ROUNDING)
async def test_redis_cache_set_many_rounds_ttl_up_to_the_millisecond(
    ttl: timedelta, px: str
) -> None:
    """`set_many` stores the TTL in whole milliseconds, rounded up."""
    # Arrange
    backend, client = _redis()

    # Act
    await backend.set_many(items={"k": b"v"}, ttl=ttl)

    # Assert
    client.eval.assert_awaited_once_with(ANY, 2, "k", "cache:rtag:k", b"v", px)


class _Connection:
    """Async context manager that yields a mock asyncpg connection."""

    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _postgres() -> tuple[PostgresCacheAdapter, MagicMock]:
    provider = PostgresProvider("postgresql://user:pass@localhost:5432/db")
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.transaction = lambda: _Connection(conn)
    pool = MagicMock()
    pool.acquire = lambda: _Connection(conn)
    provider._pool = pool
    return PostgresCacheAdapter(provider=provider), conn


@pytest.mark.parametrize(
    ("ttl", "microseconds"),
    [
        pytest.param(MICROSECOND, 1, id="one-microsecond"),
        pytest.param(timedelta(days=1), 86_400_000_000, id="one-day"),
        pytest.param(MAX_DURATION, 3_153_600_000_000_000, id="max"),
    ],
)
async def test_postgres_cache_set_passes_ttl_in_whole_microseconds(
    ttl: timedelta, microseconds: int
) -> None:
    """`set` passes the TTL as exact whole microseconds."""
    # Arrange
    backend, conn = _postgres()

    # Act
    await backend.set(key="k", value=b"v", ttl=ttl)

    # Assert
    conn.execute.assert_any_await(backend._set_sql, "k", b"v", microseconds)


async def test_postgres_cache_set_many_passes_ttl_in_whole_microseconds() -> (
    None
):
    """`set_many` passes the TTL as exact whole microseconds."""
    # Arrange
    backend, conn = _postgres()

    # Act
    await backend.set_many(items={"k": b"v"}, ttl=timedelta(milliseconds=1))

    # Assert
    conn.execute.assert_any_await(backend._set_sql, "k", b"v", 1000)


async def test_memory_cache_one_microsecond_ttl_lives_one_microsecond() -> None:
    """A one microsecond TTL holds through 999 ns and ends at 1000 ns."""
    # Arrange
    backend = MemoryCacheAdapter()
    with patch("grelmicro.cache.memory.monotonic_ns", return_value=NOW_NS):
        await backend.set(key="k", value=b"v", ttl=MICROSECOND)

    # Act
    with patch(
        "grelmicro.cache.memory.monotonic_ns", return_value=NOW_NS + 999
    ):
        last_nanosecond = await backend.get(key="k")
    with patch(
        "grelmicro.cache.memory.monotonic_ns", return_value=NOW_NS + 1000
    ):
        at_expiry = await backend.get(key="k")

    # Assert
    assert (last_nanosecond, at_expiry) == (b"v", None)


async def test_memory_cache_set_many_ttl_lives_exactly_as_asked() -> None:
    """`set_many` keeps every key through the last nanosecond of its TTL."""
    # Arrange
    backend = MemoryCacheAdapter()
    with patch("grelmicro.cache.memory.monotonic_ns", return_value=NOW_NS):
        await backend.set_many(items={"k": b"v"}, ttl=MICROSECOND)

    # Act
    with patch(
        "grelmicro.cache.memory.monotonic_ns", return_value=NOW_NS + 999
    ):
        last_nanosecond = await backend.get_many(keys=["k"])
    with patch(
        "grelmicro.cache.memory.monotonic_ns", return_value=NOW_NS + 1000
    ):
        at_expiry = await backend.get_many(keys=["k"])

    # Assert
    assert (last_nanosecond, at_expiry) == ({"k": b"v"}, {})


@given(
    now_ns=st.integers(min_value=0, max_value=4_102_444_800 * 10**9),
    ttl=st.timedeltas(min_value=MICROSECOND, max_value=MAX_DURATION),
)
def test_sqlite_cache_expiry_is_never_before_the_ttl_ends(
    now_ns: int, ttl: timedelta
) -> None:
    """The stored expiry, in float seconds, is never before the TTL ends."""
    # Arrange
    exact = Fraction(now_ns, 10**9) + Fraction(ttl // MICROSECOND, 10**6)

    # Act
    expires_at = _expires_at(now_ns, ttl)

    # Assert
    assert Fraction(expires_at) >= exact


@given(
    now_ns=st.integers(min_value=0, max_value=4_102_444_800 * 10**9),
    ttl=st.timedeltas(min_value=MICROSECOND, max_value=MAX_DURATION),
)
def test_sqlite_cache_expiry_is_within_two_microseconds_of_the_ttl_end(
    now_ns: int, ttl: timedelta
) -> None:
    """The stored expiry is less than two microseconds past the TTL end."""
    # Arrange
    exact = Fraction(now_ns, 10**9) + Fraction(ttl // MICROSECOND, 10**6)

    # Act
    expires_at = _expires_at(now_ns, ttl)

    # Assert
    assert Fraction(expires_at) < exact + Fraction(2, 10**6)
