"""Test the read-write lock adapter wiring outside the conformance suite."""

import sqlite3
from contextlib import closing
from datetime import timedelta
from types import TracebackType
from typing import Self
from unittest.mock import MagicMock

import pytest

from grelmicro.coordination.memory import MemoryReadWriteLockAdapter
from grelmicro.coordination.postgres import PostgresReadWriteLockAdapter
from grelmicro.coordination.redis import (
    RedisReadWriteLockAdapter,
    _duration_ms,
)
from grelmicro.coordination.sqlite import (
    _NOW,
    SQLiteReadWriteLockAdapter,
    _expiry,
    _seconds_text,
)
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.providers.valkey import ValkeyProvider

pytestmark = [pytest.mark.timeout(10)]

REDIS_URL = "redis://:test_password@test_host:1234/0"
POSTGRES_URL = "postgresql://test:test@test_host:5432/test"


class _StubProvider:
    """Minimal provider-shaped stub tracking enter and exit calls."""

    is_cluster = False

    def __init__(self) -> None:
        self.client = MagicMock()
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> Self:
        self.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_count += 1


# --- Redis ---


def test_redis_implicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)

    adapter = RedisReadWriteLockAdapter()

    assert adapter.provider.url == REDIS_URL
    assert adapter._owns_provider is True


def test_redis_rebind_provider() -> None:
    """A rebound provider is borrowed and the scripts follow it."""
    adapter = RedisReadWriteLockAdapter(provider=RedisProvider(REDIS_URL))
    other = RedisProvider(REDIS_URL)

    adapter._rebind_provider(other)

    assert adapter.provider is other
    assert adapter._owns_provider is False


async def test_redis_owned_provider_opens_and_closes() -> None:
    """When owned, the adapter opens and closes its provider."""
    stub = _StubProvider()
    adapter = RedisReadWriteLockAdapter(provider=stub)  # ty: ignore[invalid-argument-type]
    adapter._owns_provider = True

    async with adapter:
        pass

    assert stub.enter_count == 1
    assert stub.exit_count == 1


def test_redis_provider_factory() -> None:
    """`RedisProvider.readwritelock_backend()` returns a bound adapter."""
    provider = RedisProvider(REDIS_URL)

    adapter = provider.readwritelock_backend()

    assert isinstance(adapter, RedisReadWriteLockAdapter)
    assert adapter.provider is provider


def test_valkey_provider_factory() -> None:
    """`ValkeyProvider.readwritelock_backend()` reuses the Redis adapter."""
    provider = ValkeyProvider("valkey://test_host:1234/0")

    adapter = provider.readwritelock_backend()

    assert isinstance(adapter, RedisReadWriteLockAdapter)
    assert adapter.provider is provider


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(milliseconds=1500), 1500),
        (timedelta(milliseconds=1001), 1001),
        (timedelta(microseconds=1_000_001), 1001),
        (timedelta(microseconds=1), 1),
    ],
)
def test_redis_duration_rounds_up_to_the_millisecond(
    duration: timedelta, expected: int
) -> None:
    """A lease reaches Redis in whole milliseconds, rounded up, never zero."""
    assert _duration_ms(duration) == expected


# --- SQLite ---


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(seconds=60), "60.000"),
        (timedelta(milliseconds=1001), "1.001"),
        (timedelta(microseconds=1_000_001), "1.001"),
        (timedelta(microseconds=1), "0.001"),
    ],
)
def test_sqlite_duration_rounds_up_to_the_millisecond(
    duration: timedelta, expected: str
) -> None:
    """A lease reaches SQLite in seconds to the millisecond, rounded up."""
    assert _seconds_text(duration) == expected


def test_sqlite_expiry_in_whole_seconds_holds_through_its_second() -> None:
    """An expiry written in whole seconds holds until that second ends."""
    with closing(sqlite3.connect(":memory:")) as conn:
        row = conn.execute(
            f"SELECT {_expiry('this')} >= {_NOW},"  # noqa: S608
            f" {_expiry('last')} >= {_NOW},"
            f" {_expiry('ms')} >= {_NOW}"
            " FROM (SELECT datetime('now') AS this,"
            " datetime('now', '-1 seconds') AS last,"
            " strftime('%Y-%m-%d %H:%M:%f', 'now', '-0.001 seconds') AS ms)"
        ).fetchone()
    assert row == (1, 0, 0)


# --- Postgres ---


def test_postgres_table_name_must_be_an_identifier() -> None:
    """A table name that is not an identifier is refused at construction."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        PostgresReadWriteLockAdapter(table_name="rw; DROP TABLE users")


def test_postgres_implicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", POSTGRES_URL)

    adapter = PostgresReadWriteLockAdapter()

    assert adapter._owns_provider is True


def test_postgres_rebind_provider() -> None:
    """A rebound provider is borrowed rather than owned."""
    adapter = PostgresReadWriteLockAdapter(
        provider=PostgresProvider(POSTGRES_URL)
    )
    other = PostgresProvider(POSTGRES_URL)

    adapter._rebind_provider(other)

    assert adapter.provider is other
    assert adapter._owns_provider is False


async def test_postgres_owned_provider_opens_and_closes() -> None:
    """When owned, the adapter opens and closes its provider."""
    stub = _StubProvider()
    adapter = PostgresReadWriteLockAdapter(
        provider=stub,  # ty: ignore[invalid-argument-type]
        auto_migrate=False,
    )
    adapter._owns_provider = True

    async with adapter:
        pass

    assert stub.enter_count == 1
    assert stub.exit_count == 1


def test_postgres_provider_factory() -> None:
    """`PostgresProvider.readwritelock_backend()` returns a bound adapter."""
    provider = PostgresProvider(POSTGRES_URL)

    adapter = provider.readwritelock_backend()

    assert isinstance(adapter, PostgresReadWriteLockAdapter)
    assert adapter.provider is provider


# --- SQLite and Memory ---


def test_sqlite_provider_factory() -> None:
    """`SQLiteProvider.readwritelock_backend()` returns a bound adapter."""
    provider = SQLiteProvider(":memory:")

    adapter = provider.readwritelock_backend()

    assert isinstance(adapter, SQLiteReadWriteLockAdapter)
    assert adapter.provider is provider


def test_memory_provider_caches_one_adapter() -> None:
    """`MemoryProvider.readwritelock_backend()` hands back one shared adapter."""
    provider = MemoryProvider()

    first = provider.readwritelock_backend()
    second = provider.readwritelock_backend()

    assert isinstance(first, MemoryReadWriteLockAdapter)
    assert first is second
