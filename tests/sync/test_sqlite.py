"""Tests for SQLite Backends."""

import pytest

from grelmicro.errors import OutOfContextError
from grelmicro.sync.errors import SyncSettingsValidationError
from grelmicro.sync.sqlite import SQLiteSyncAdapter

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
        SQLiteSyncAdapter(path=":memory:", table_name=table_name)


async def test_sync_backend_out_of_context_errors() -> None:
    """Test Synchronization Backend Out Of Context Errors."""
    # Arrange
    backend = SQLiteSyncAdapter(path=":memory:")
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


def test_sqlite_env_var_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test SQLite Settings from Environment Variables."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", "locks.db")

    # Act
    backend = SQLiteSyncAdapter()

    # Assert
    assert backend._path == "locks.db"


def test_sqlite_env_var_settings_validation_error() -> None:
    """Test SQLite Settings Validation Error."""
    # Assert / Act
    with pytest.raises(
        SyncSettingsValidationError,
        match=(r"Could not validate settings:\n"),
    ):
        SQLiteSyncAdapter()


def test_sync_backend_custom_table_name() -> None:
    """Test Synchronization Backend Custom Table Name."""
    # Act
    backend = SQLiteSyncAdapter(path=":memory:", table_name="my_locks")

    # Assert
    assert backend._table_name == "my_locks"
