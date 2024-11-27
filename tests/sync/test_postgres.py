"""Tests for PostgreSQL Backends."""

import pytest

from grelmicro.errors import OutOfContextError
from grelmicro.sync.postgres import PostgresSyncBackend

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(1)]

URL = "postgres://user:password@localhost:5432/db"


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
        ValueError, match="Table name '.*' is not a valid identifier"
    ):
        PostgresSyncBackend(url=URL, table_name=table_name)


async def test_sync_backend_out_of_context_errors() -> None:
    """Test Synchronization Backend Out Of Context Errors."""
    # Arrange
    backend = PostgresSyncBackend(url=URL)
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
