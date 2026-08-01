"""Shared asyncpg pool fakes for the Postgres adapter tests.

Every Postgres adapter installs its schema inside
`pool.acquire()` plus `conn.transaction()`, so it can hold an advisory
lock for the whole migration. A fake pool therefore has to support both,
and the statements land on the connection rather than the pool.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock


class _Transaction:
    """Async context manager standing in for `conn.transaction()`."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Acquire:
    """Async context manager standing in for `pool.acquire()`."""

    def __init__(self, conn: MagicMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> MagicMock:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def mock_connection() -> MagicMock:
    """Return a connection whose `execute` and `transaction` both work."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()
    conn.transaction = _Transaction
    return conn


def mock_pool(conn: MagicMock | None = None) -> Any:  # noqa: ANN401
    """Return a pool that hands out `conn` and records what ran on it."""
    connection = conn if conn is not None else mock_connection()
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.acquire = lambda: _Acquire(connection)
    pool.connection = connection
    return pool


def executed_statements(pool: Any) -> list[str]:  # noqa: ANN401
    """Return the SQL run on the pool's connection, in order."""
    return [call.args[0] for call in pool.connection.execute.await_args_list]
