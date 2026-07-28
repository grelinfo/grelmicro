"""Tests for SQLite Backends."""

from pathlib import Path

import aiosqlite
import pytest

from grelmicro.cache.sqlite import SQLiteCacheAdapter
from grelmicro.coordination.sqlite import SQLiteLockAdapter
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(1)]


@pytest.mark.parametrize(
    "table_name",
    [
        "locks table",
        "%locks",
        "locks;table",
        "locks' OR '1'='1",
        "locks; DROP TABLE users; --",
    ],
)
def test_sync_backend_table_name_invalid(table_name: str) -> None:
    """Test Synchronization Backend Table Name Invalid."""
    # Act / Assert
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        SQLiteLockAdapter(
            provider=SQLiteProvider(":memory:"), table_name=table_name
        )


async def test_sync_backend_out_of_context_errors() -> None:
    """Test Synchronization Backend Out Of Context Errors."""
    # Arrange
    backend = SQLiteLockAdapter(provider=SQLiteProvider(":memory:"))
    name = "lock"
    key = "token"

    # Act / Assert
    with pytest.raises(OutOfContextError):
        await backend.acquire(name=name, token=key, duration=1)
    with pytest.raises(OutOfContextError):
        await backend.release(name=name, token=key)
    with pytest.raises(OutOfContextError):
        await backend.locked(name=name)
    with pytest.raises(OutOfContextError):
        await backend.owned(name=name, token=key)


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds one from `SQLITE_PATH`."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", "locks.db")

    # Act
    backend = SQLiteLockAdapter()

    # Assert
    assert backend.provider.path == "locks.db"
    assert backend._owns_provider is True


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    # Arrange
    provider = SQLiteProvider("locks.db")

    # Act
    backend = SQLiteLockAdapter(provider=provider)

    # Assert
    assert backend.provider is provider
    assert backend._owns_provider is False


def test_rebind_provider_borrows() -> None:
    """`_rebind_provider` swaps the provider and marks it borrowed."""
    # Arrange
    backend = SQLiteLockAdapter(provider=SQLiteProvider("a.db"))
    shared = SQLiteProvider("shared.db")

    # Act
    backend._rebind_provider(shared)

    # Assert
    assert backend.provider is shared
    assert backend._owns_provider is False


def test_sqlite_env_var_settings_validation_error() -> None:
    """Test SQLite Settings Validation Error."""
    # Assert / Act
    with pytest.raises(SettingsValidationError, match="SQLITE_PATH"):
        SQLiteLockAdapter()


def test_sync_backend_custom_table_name() -> None:
    """Test Synchronization Backend Custom Table Name."""
    # Act
    backend = SQLiteLockAdapter(
        provider=SQLiteProvider(":memory:"), table_name="my_locks"
    )

    # Assert
    assert backend._table_name == "my_locks"


async def test_owned_provider_opens_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implicit (owned) provider is opened on enter and closed on exit."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "owned.db"))
    backend = SQLiteLockAdapter()  # builds and owns the provider

    # Act
    async with backend:
        fence = await backend.acquire(name="lock", token="token", duration=1)

    # Assert
    assert fence == 1
    with pytest.raises(OutOfContextError):
        _ = backend.provider.client


async def test_borrowed_provider_stays_open(tmp_path: Path) -> None:
    """A borrowed provider outlives the adapter that used it."""
    # Arrange
    provider = SQLiteProvider(tmp_path / "shared.db")

    # Act
    async with provider:
        async with SQLiteLockAdapter(provider=provider) as backend:
            await backend.acquire(name="lock", token="token", duration=1)

        # Assert
        assert provider.client is not None


async def test_shares_one_connection_with_another_component(
    tmp_path: Path,
) -> None:
    """A lock and a cache on one provider run on the same connection."""
    # Arrange
    provider = SQLiteProvider(tmp_path / "shared.db")

    # Act
    async with (
        provider,
        SQLiteLockAdapter(provider=provider) as lock,
        SQLiteCacheAdapter(provider=provider) as cache,
    ):
        await lock.acquire(name="lock", token="token", duration=1)
        await cache.set(key="k", value=b"v", ttl=10)

        # Assert
        assert lock.provider.client is cache.provider.client


async def test_acquire_and_release_commit_for_other_processes(
    tmp_path: Path,
) -> None:
    """Acquire and release are durable, not left open on the connection.

    The provider's connection runs in autocommit, so both statements commit
    when they finish. A second connection stands in for another process: it
    can only see writes that actually committed, which an assertion on the
    adapter's own connection could not distinguish.
    """
    # Arrange
    path = tmp_path / "locks.db"
    provider = SQLiteProvider(path)

    async with provider, SQLiteLockAdapter(provider=provider) as backend:
        # Act
        await backend.acquire(name="lock", token="token", duration=60)

        # Assert
        async with (
            aiosqlite.connect(path) as other,
            other.execute("SELECT token FROM locks WHERE name = 'lock';") as c,
        ):
            assert await c.fetchone() == ("token",)

        # Act
        released = await backend.release(name="lock", token="token")

        # Assert
        assert released is True
        async with (
            aiosqlite.connect(path) as other,
            other.execute("SELECT token FROM locks WHERE name = 'lock';") as c,
        ):
            assert await c.fetchone() == (None,)
        assert provider.client.in_transaction is False


async def test_acquire_rolls_back_on_error(tmp_path: Path) -> None:
    """A failing acquire rolls back the open transaction and re-raises."""
    # Arrange
    provider = SQLiteProvider(tmp_path / "locks.db")
    async with provider, SQLiteLockAdapter(provider=provider) as backend:
        conn = provider.client
        await conn.execute("DROP TABLE locks;")

        # Act / Assert
        with pytest.raises(Exception, match="no such table"):
            await backend.acquire(name="lock", token="token", duration=1)

        assert conn.in_transaction is False

        # Restore the schema so the context manager exit can run cleanly.
        await conn.execute(
            SQLiteLockAdapter._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(
                table_name="locks"
            )
        )
