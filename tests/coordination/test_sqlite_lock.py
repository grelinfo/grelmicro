"""Tests for SQLite Backends."""

from pathlib import Path

import pytest

from grelmicro.coordination.errors import CoordinationSettingsValidationError
from grelmicro.coordination.sqlite import SQLiteLockAdapter
from grelmicro.errors import OutOfContextError

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
        SQLiteLockAdapter(path=":memory:", table_name=table_name)


async def test_sync_backend_out_of_context_errors() -> None:
    """Test Synchronization Backend Out Of Context Errors."""
    # Arrange
    backend = SQLiteLockAdapter(path=":memory:")
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
    backend = SQLiteLockAdapter()

    # Assert
    assert backend._path == "locks.db"


def test_sqlite_env_var_settings_validation_error() -> None:
    """Test SQLite Settings Validation Error."""
    # Assert / Act
    with pytest.raises(
        CoordinationSettingsValidationError,
        match=(r"Could not validate settings:\n"),
    ):
        SQLiteLockAdapter()


def test_sync_backend_custom_table_name() -> None:
    """Test Synchronization Backend Custom Table Name."""
    # Act
    backend = SQLiteLockAdapter(path=":memory:", table_name="my_locks")

    # Assert
    assert backend._table_name == "my_locks"


async def test_acquire_rolls_back_on_error(tmp_path: Path) -> None:
    """A failing acquire rolls back the open transaction and re-raises."""
    # Arrange
    backend = SQLiteLockAdapter(tmp_path / "locks.db")
    async with backend:
        conn = backend._conn
        assert conn is not None
        await conn.execute("DROP TABLE locks;")
        await conn.commit()

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
        await conn.commit()
