"""SQLite Synchronization Backend."""

from pathlib import Path
from time import time
from types import TracebackType
from typing import Annotated, Self

import aiosqlite
from pydantic_settings import BaseSettings
from typing_extensions import Doc

from grelmicro.errors import OutOfContextError
from grelmicro.sync._backends import loaded_backends
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import SyncSettingsValidationError


class _SQLiteSettings(BaseSettings):
    """SQLite settings from the environment variables."""

    SQLITE_PATH: str | None = None


def _get_sqlite_path() -> str:
    """Get the SQLite path from the environment variables.

    Raises:
        SyncSettingsValidationError: If SQLITE_PATH is not set.
    """
    settings = _SQLiteSettings()

    if settings.SQLITE_PATH:
        return settings.SQLITE_PATH

    msg = "SQLITE_PATH must be set"
    raise SyncSettingsValidationError(msg)


class SQLiteSyncBackend(SyncBackend):
    """SQLite Synchronization Backend."""

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
                CREATE TABLE IF NOT EXISTS {table_name} (
                    name TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    expire_at REAL NOT NULL
                );
                """

    _SQL_ACQUIRE_OR_EXTEND = """
                INSERT INTO {table_name} (name, token, expire_at)
                VALUES (?, ?, ?)
                ON CONFLICT (name) DO UPDATE
                SET token = EXCLUDED.token, expire_at = EXCLUDED.expire_at
                WHERE {table_name}.token = EXCLUDED.token
                   OR {table_name}.expire_at < ?
                RETURNING 1;
                """

    _SQL_RELEASE = """
            DELETE FROM {table_name}
            WHERE name = ? AND token = ? AND expire_at >= ?
            RETURNING 1;
            """

    _SQL_RELEASE_ALL_EXPIRED = """
        DELETE FROM {table_name}
        WHERE expire_at < ?;
        """

    _SQL_LOCKED = """
        SELECT 1 FROM {table_name}
        WHERE name = ? AND expire_at >= ?;
        """

    _SQL_OWNED = """
        SELECT 1 FROM {table_name}
        WHERE name = ? AND token = ? AND expire_at >= ?;
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
        auto_register: Annotated[
            bool,
            Doc(
                "Automatically register the lock backend in the backend registry."
            ),
        ] = True,
        table_name: Annotated[
            str, Doc("The table name to store the locks.")
        ] = "locks",
    ) -> None:
        """Initialize the lock backend."""
        if not table_name.isidentifier():
            msg = f"Table name '{table_name}' is not a valid identifier"
            raise ValueError(msg)

        self._path = str(path) if path is not None else _get_sqlite_path()
        self._table_name = table_name
        self._acquire_sql = self._SQL_ACQUIRE_OR_EXTEND.format(
            table_name=table_name
        )
        self._release_sql = self._SQL_RELEASE.format(table_name=table_name)
        self._conn: aiosqlite.Connection | None = None
        if auto_register:
            loaded_backends["lock"] = self

    async def __aenter__(self) -> Self:
        """Enter the lock backend."""
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
        """Exit the lock backend."""
        if self._conn:
            await self._conn.execute(
                self._SQL_RELEASE_ALL_EXPIRED.format(
                    table_name=self._table_name
                ),
                (time(),),
            )
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire a lock."""
        if not self._conn:
            raise OutOfContextError(self, "acquire")

        now = time()
        async with self._conn.execute(
            self._acquire_sql, (name, token, now + duration, now)
        ) as cursor:
            result = await cursor.fetchone()
        await self._conn.commit()
        return result is not None

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        if not self._conn:
            raise OutOfContextError(self, "release")

        async with self._conn.execute(
            self._release_sql, (name, token, time())
        ) as cursor:
            result = await cursor.fetchone()
        await self._conn.commit()
        return result is not None

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        if not self._conn:
            raise OutOfContextError(self, "locked")

        async with self._conn.execute(
            self._SQL_LOCKED.format(table_name=self._table_name),
            (name, time()),
        ) as cursor:
            result = await cursor.fetchone()
        return result is not None

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        if not self._conn:
            raise OutOfContextError(self, "owned")

        async with self._conn.execute(
            self._SQL_OWNED.format(table_name=self._table_name),
            (name, token, time()),
        ) as cursor:
            result = await cursor.fetchone()
        return result is not None
