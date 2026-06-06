"""Tests for the Postgres Cache Adapter."""

import asyncio
from types import TracebackType
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from grelmicro.cache.postgres import PostgresCacheAdapter, _escape_like
from grelmicro.providers.postgres import (
    PostgresProvider,
    PostgresProviderConfigError,
)

pytestmark = [pytest.mark.timeout(1)]

URL = "postgresql://test_user:test_password@test_host:1234/test_db"


def _mock_conn() -> MagicMock:
    """Return a mock asyncpg connection with an async transaction()."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()

    class _Txn:
        async def __aenter__(self) -> object:
            return conn

        async def __aexit__(self, *exc: object) -> None:
            return None

    conn.transaction = _Txn
    return conn


def _acquire_cm(conn: MagicMock) -> object:
    """Return an async context manager yielding the given connection."""

    class _Acquire:
        async def __aenter__(self) -> MagicMock:
            return conn

        async def __aexit__(self, *exc: object) -> None:
            return None

    return _Acquire()


def _build_with_mock_pool(
    *,
    prefix: str = "",
    auto_migrate: bool = True,
    cleanup_interval: float | None = None,
) -> tuple[PostgresCacheAdapter, MagicMock]:
    """Return an adapter wired to a mocked asyncpg pool via a provider."""
    provider = PostgresProvider(URL)
    mock_pool = MagicMock()
    provider._pool = mock_pool
    backend = PostgresCacheAdapter(
        provider=provider,
        prefix=prefix,
        auto_migrate=auto_migrate,
        cleanup_interval=cleanup_interval,
    )
    return backend, mock_pool


class _StubProvider:
    """Minimal `PostgresProvider`-shaped stub tracking enter/exit calls."""

    def __init__(self) -> None:
        self.client = MagicMock()
        self.client.execute = AsyncMock()
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


@pytest.mark.parametrize(
    "table_name",
    [
        "cache table",
        "%cache",
        "cache;table",
        "cache' OR '1'='1",
        "cache; DROP TABLE users; --",
    ],
)
def test_table_name_invalid(table_name: str) -> None:
    """Invalid SQL identifiers for the table name raise."""
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        PostgresCacheAdapter(
            provider=PostgresProvider(URL), table_name=table_name
        )


def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    backend = PostgresCacheAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = PostgresProvider(URL)
    backend = PostgresCacheAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("WRITE_POSTGRES_URL", URL)

    backend = PostgresCacheAdapter(env_prefix="WRITE_POSTGRES_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "WRITE_POSTGRES_"


def test_env_validation_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit provider surfaces `PostgresProviderConfigError`."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    with pytest.raises(PostgresProviderConfigError):
        PostgresCacheAdapter()


def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    backend = PostgresCacheAdapter(
        provider=PostgresProvider(URL), table_name="my_cache"
    )

    assert backend._table_name == "my_cache"


def test_prefix_stored() -> None:
    """`prefix=` is stored on the adapter."""
    backend = PostgresCacheAdapter(
        provider=PostgresProvider(URL), prefix="myapp:"
    )

    assert backend._key_prefix == "myapp:"


def test_auto_migrate_flag() -> None:
    """`auto_migrate=` is stored on the adapter."""
    backend = PostgresCacheAdapter(
        provider=PostgresProvider(URL), auto_migrate=False
    )

    assert backend._auto_migrate is False


@pytest.mark.parametrize("interval", [0, 0.0, -1, -0.5])
def test_cleanup_interval_non_positive_raises(interval: float) -> None:
    """`cleanup_interval` must be positive when set."""
    with pytest.raises(ValueError, match="cleanup_interval must be positive"):
        PostgresCacheAdapter(
            provider=PostgresProvider(URL), cleanup_interval=interval
        )


def test_cleanup_interval_default_off() -> None:
    """The janitor is off by default."""
    backend = PostgresCacheAdapter(provider=PostgresProvider(URL))

    assert backend._cleanup_interval is None


def test_cleanup_interval_stored() -> None:
    """`cleanup_interval=` is stored on the adapter."""
    interval = 60.0
    backend = PostgresCacheAdapter(
        provider=PostgresProvider(URL), cleanup_interval=interval
    )

    assert backend._cleanup_interval == interval


def test_provider_cache_factory() -> None:
    """`PostgresProvider.cache()` builds a `PostgresCacheAdapter`."""
    provider = PostgresProvider(URL)

    backend = provider.cache()

    assert isinstance(backend, PostgresCacheAdapter)
    assert backend.provider is provider
    assert backend._owns_provider is False


def test_rebind_provider_borrows_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    monkeypatch.setenv("POSTGRES_URL", URL)
    backend = PostgresCacheAdapter()
    assert backend._owns_provider is True
    other = PostgresProvider(URL)

    backend._rebind_provider(other)

    assert backend.provider is other
    assert backend._owns_provider is False


def test_escape_like_special_chars() -> None:
    r"""`_escape_like` escapes `%`, `_`, and `\`."""
    assert _escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"


class TestAsyncMethods:
    """Tests using a mocked asyncpg pool."""

    async def test_get_hit(self) -> None:
        """`get` returns the stored bytes when the key exists."""
        backend, pool = _build_with_mock_pool(prefix="p:")
        pool.fetchrow = AsyncMock(return_value={"value": b"value"})

        result = await backend.get(key="k")

        assert result == b"value"
        pool.fetchrow.assert_awaited_once_with(backend._get_sql, "p:k")

    async def test_get_miss_returns_none(self) -> None:
        """`get` returns None when the key is missing."""
        backend, pool = _build_with_mock_pool()
        pool.fetchrow = AsyncMock(return_value=None)

        assert await backend.get(key="missing") is None

    async def test_set_passes_key_value_ttl(self) -> None:
        """`set` upserts the value and clears stale tags in a transaction."""
        backend, pool = _build_with_mock_pool(prefix="p:")
        conn = _mock_conn()
        pool.acquire = lambda: _acquire_cm(conn)

        await backend.set(key="k", value=b"v", ttl=30)

        conn.execute.assert_any_await(backend._set_sql, "p:k", b"v", 30.0)
        conn.execute.assert_any_await(backend._delete_tags_of_key_sql, "p:k")
        conn.executemany.assert_not_awaited()

    async def test_set_with_tags_inserts_tag_rows(self) -> None:
        """`set` with tags inserts a tag row per tag in the transaction."""
        backend, pool = _build_with_mock_pool(prefix="p:")
        conn = _mock_conn()
        pool.acquire = lambda: _acquire_cm(conn)

        await backend.set(key="k", value=b"v", ttl=30, tags=["t1", "t2"])

        conn.executemany.assert_awaited_once_with(
            backend._insert_tag_sql, [("p:k", "t1"), ("p:k", "t2")]
        )

    async def test_delete(self) -> None:
        """`delete` forwards the prefixed key."""
        backend, pool = _build_with_mock_pool(prefix="p:")
        pool.execute = AsyncMock()

        await backend.delete(key="k")

        pool.execute.assert_awaited_once_with(backend._delete_sql, "p:k")

    async def test_clear_with_prefix(self) -> None:
        """`clear` issues a prefix-scoped delete when a prefix is set."""
        backend, pool = _build_with_mock_pool(prefix="p:")
        pool.execute = AsyncMock()

        await backend.clear()

        pool.execute.assert_awaited_once_with(backend._clear_prefix_sql, "p:%")

    async def test_clear_without_prefix(self) -> None:
        """`clear` issues a full-table delete when no prefix is set."""
        backend, pool = _build_with_mock_pool()
        pool.execute = AsyncMock()

        await backend.clear()

        pool.execute.assert_awaited_once_with(backend._clear_all_sql)

    async def test_aenter_owned_provider_opens_it(self) -> None:
        """`__aenter__` opens the provider when the adapter owns it."""
        stub = _StubProvider()
        backend = PostgresCacheAdapter.__new__(PostgresCacheAdapter)
        backend._provider = stub  # type: ignore[assignment]
        backend._owns_provider = True
        backend._auto_migrate = False
        backend._cleanup_interval = None
        backend._janitor_task = None
        backend._loop = None

        async with backend:
            pass

        assert stub.enter_count == 1
        assert stub.exit_count == 1

    async def test_aenter_runs_migration(self) -> None:
        """`__aenter__` runs the create-table SQL when `auto_migrate=True`."""
        backend, pool = _build_with_mock_pool()
        pool.execute = AsyncMock()

        async with backend:
            pass

        pool.execute.assert_awaited_once()
        call = pool.execute.await_args
        assert call is not None
        assert "CREATE TABLE IF NOT EXISTS grelmicro_cache" in call.args[0]

    async def test_aenter_skips_migration_when_disabled(self) -> None:
        """`auto_migrate=False` skips schema installation."""
        backend, pool = _build_with_mock_pool(auto_migrate=False)
        pool.execute = AsyncMock()

        async with backend:
            pass

        pool.execute.assert_not_awaited()

    async def test_janitor_starts_and_stops(self) -> None:
        """Setting `cleanup_interval` spawns and cancels a janitor task."""
        backend, pool = _build_with_mock_pool(
            cleanup_interval=0.01, auto_migrate=False
        )
        pool.execute = AsyncMock()

        async with backend:
            assert backend._janitor_task is not None
            await asyncio.sleep(0.05)

        assert backend._janitor_task is None
        assert pool.execute.await_count >= 1
        call = pool.execute.await_args
        assert call is not None
        assert "DELETE FROM grelmicro_cache" in call.args[0]

    async def test_janitor_suppresses_errors(self) -> None:
        """Janitor swallows errors so transient failures don't crash the task."""
        backend, pool = _build_with_mock_pool(
            cleanup_interval=0.01, auto_migrate=False
        )
        pool.execute = AsyncMock(side_effect=RuntimeError("boom"))

        async with backend:
            await asyncio.sleep(0.05)

        assert pool.execute.await_count >= 1
