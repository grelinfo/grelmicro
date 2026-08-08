"""Test the read-write lock adapter wiring outside the conformance suite."""

from types import TracebackType
from typing import Self
from unittest.mock import MagicMock

import pytest

from grelmicro.coordination.memory import MemoryReadWriteLockAdapter
from grelmicro.coordination.postgres import PostgresReadWriteLockAdapter
from grelmicro.coordination.redis import RedisReadWriteLockAdapter
from grelmicro.coordination.sqlite import SQLiteReadWriteLockAdapter
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
    """`RedisProvider.readwritelock()` returns a bound adapter."""
    provider = RedisProvider(REDIS_URL)

    adapter = provider.readwritelock()

    assert isinstance(adapter, RedisReadWriteLockAdapter)
    assert adapter.provider is provider


def test_valkey_provider_factory() -> None:
    """`ValkeyProvider.readwritelock()` reuses the Redis adapter."""
    provider = ValkeyProvider("valkey://test_host:1234/0")

    adapter = provider.readwritelock()

    assert isinstance(adapter, RedisReadWriteLockAdapter)
    assert adapter.provider is provider


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
    """`PostgresProvider.readwritelock()` returns a bound adapter."""
    provider = PostgresProvider(POSTGRES_URL)

    adapter = provider.readwritelock()

    assert isinstance(adapter, PostgresReadWriteLockAdapter)
    assert adapter.provider is provider


# --- SQLite and Memory ---


def test_sqlite_provider_factory() -> None:
    """`SQLiteProvider.readwritelock()` returns a bound adapter."""
    provider = SQLiteProvider(":memory:")

    adapter = provider.readwritelock()

    assert isinstance(adapter, SQLiteReadWriteLockAdapter)
    assert adapter.provider is provider


def test_memory_provider_caches_one_adapter() -> None:
    """`MemoryProvider.readwritelock()` hands back one shared adapter."""
    provider = MemoryProvider()

    first = provider.readwritelock()
    second = provider.readwritelock()

    assert isinstance(first, MemoryReadWriteLockAdapter)
    assert first is second
