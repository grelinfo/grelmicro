"""SQLite Lock Adapter."""

import asyncio
import re
from math import ceil
from pathlib import Path
from types import TracebackType
from typing import Annotated, Self

import aiosqlite
from pydantic_settings import BaseSettings
from typing_extensions import Doc

from grelmicro.coordination.abc import LockBackend
from grelmicro.coordination.errors import CoordinationSettingsValidationError
from grelmicro.errors import OutOfContextError


class _SQLiteSettings(BaseSettings):
    """SQLite settings from the environment variables."""

    SQLITE_PATH: str | None = None


def _get_sqlite_path() -> str:
    """Get the SQLite path from the environment variables.

    Raises:
        CoordinationSettingsValidationError: If SQLITE_PATH is not set.
    """
    settings = _SQLiteSettings()

    if settings.SQLITE_PATH:
        return settings.SQLITE_PATH

    msg = "SQLITE_PATH must be set"
    raise CoordinationSettingsValidationError(msg)


class SQLiteLockAdapter(LockBackend):
    """SQLite Lock Adapter.

    Fencing tokens live in a `fence` column on the lock row. Acquire runs
    inside a `BEGIN IMMEDIATE` transaction, bumps the fence on every
    free-to-held transition, keeps it on a same-holder extend, and returns it
    with `RETURNING fence`. Release clears the holder and expiry but keeps the
    row and its fence, so the fence is strictly monotonic per name across
    release and re-acquire cycles.
    """

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
                CREATE TABLE IF NOT EXISTS {table_name} (
                    name TEXT PRIMARY KEY,
                    token TEXT,
                    expire_at TEXT,
                    fence INTEGER NOT NULL DEFAULT 0
                );
                """

    _SQL_ACQUIRE_OR_EXTEND = """
                INSERT INTO {table_name} (name, token, expire_at, fence)
                VALUES (
                    ?, ?, datetime('now', '+' || ? || ' seconds'), 1
                )
                ON CONFLICT (name) DO UPDATE
                SET token = EXCLUDED.token,
                    expire_at = EXCLUDED.expire_at,
                    fence = CASE
                        WHEN {table_name}.token = EXCLUDED.token
                             AND {table_name}.expire_at >= datetime('now')
                        THEN {table_name}.fence
                        ELSE {table_name}.fence + 1
                    END
                WHERE {table_name}.token = EXCLUDED.token
                   OR {table_name}.token IS NULL
                   OR {table_name}.expire_at IS NULL
                   OR {table_name}.expire_at < datetime('now')
                RETURNING fence;
                """

    _SQL_RELEASE = """
            UPDATE {table_name}
            SET token = NULL, expire_at = NULL
            WHERE name = ? AND token = ? AND expire_at >= datetime('now')
            RETURNING 1;
            """

    _SQL_RELEASE_ALL_EXPIRED = """
        UPDATE {table_name}
        SET token = NULL, expire_at = NULL
        WHERE expire_at < datetime('now');
        """

    _SQL_LOCKED = """
        SELECT 1 FROM {table_name}
        WHERE name = ? AND token IS NOT NULL AND expire_at >= datetime('now');
        """

    _SQL_OWNED = """
        SELECT 1 FROM {table_name}
        WHERE name = ? AND token = ? AND expire_at >= datetime('now');
        """

    def __init__(
        self,
        path: Annotated[
            str | Path | None,
            Doc("""
                The SQLite database path.

                If not provided, the path will be taken from the environment variable SQLITE_PATH.
                """),
        ] = None,
        *,
        table_name: Annotated[
            str, Doc("The table name to store the locks.")
        ] = "locks",
    ) -> None:
        """Initialize the lock backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise ValueError(msg)

        self._path = str(path) if path is not None else _get_sqlite_path()
        self._table_name = table_name
        self._acquire_sql = self._SQL_ACQUIRE_OR_EXTEND.format(
            table_name=table_name
        )
        self._release_sql = self._SQL_RELEASE.format(table_name=table_name)
        self._locked_sql = self._SQL_LOCKED.format(table_name=table_name)
        self._owned_sql = self._SQL_OWNED.format(table_name=table_name)
        self._conn: aiosqlite.Connection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the lock backend."""
        self._loop = asyncio.get_running_loop()
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute(
            self._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(
                table_name=self._table_name
            ),
        )
        await self._conn.commit()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lock backend."""
        if self._conn:  # pragma: no branch
            await self._conn.execute(
                self._SQL_RELEASE_ALL_EXPIRED.format(
                    table_name=self._table_name
                ),
            )
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a lock, returning the fencing token or `None`.

        Runs the read-modify-write inside a `BEGIN IMMEDIATE` transaction so
        the fence high-water update is serialized against concurrent writers.
        """
        if not self._conn:
            raise OutOfContextError(self, "acquire")

        await self._conn.execute("BEGIN IMMEDIATE;")
        try:
            async with self._conn.execute(
                self._acquire_sql, (name, token, ceil(duration))
            ) as cursor:
                result = await cursor.fetchone()
        except BaseException:
            await self._conn.rollback()
            raise
        await self._conn.commit()
        return int(result[0]) if result is not None else None

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        if not self._conn:
            raise OutOfContextError(self, "release")

        async with self._conn.execute(
            self._release_sql, (name, token)
        ) as cursor:
            result = await cursor.fetchone()
        await self._conn.commit()
        return result is not None

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        if not self._conn:
            raise OutOfContextError(self, "locked")

        async with self._conn.execute(self._locked_sql, (name,)) as cursor:
            result = await cursor.fetchone()
        return result is not None

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        if not self._conn:
            raise OutOfContextError(self, "owned")

        async with self._conn.execute(self._owned_sql, (name, token)) as cursor:
            result = await cursor.fetchone()
        return result is not None
