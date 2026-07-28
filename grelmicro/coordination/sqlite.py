"""SQLite coordination adapters."""

from __future__ import annotations

import asyncio
import re
from math import ceil
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.coordination._protocol import LockBackend, ScheduleBackend
from grelmicro.providers.sqlite import SQLiteProvider

if TYPE_CHECKING:
    from types import TracebackType


class SQLiteLockAdapter(LockBackend):
    """SQLite Lock Adapter.

    Borrows the connection and a shared lock from a `SQLiteProvider` and
    implements the `LockBackend` protocol for distributed locks on a single
    host. Pass an explicit `provider=` to share a connection with other
    components, or rely on the default `env_prefix=` to build one from
    environment variables.

    Fencing tokens live in a `fence` column on the lock row. Acquire runs
    inside a `BEGIN IMMEDIATE` transaction, bumps the fence on every
    free-to-held transition, keeps it on a same-holder extend, and returns it
    with `RETURNING fence`. Release clears the holder and expiry but keeps the
    row and its fence, so the fence is strictly monotonic per name across
    release and re-acquire cycles.

    The provider's lock serializes the single connection within the process,
    and the transaction's write lock serializes across processes sharing the
    same file.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.coordination import Coordination
    from grelmicro.providers.sqlite import SQLiteProvider

    sqlite = SQLiteProvider("app.db")
    micro = Grelmicro(uses=[sqlite, Coordination(lock=sqlite)])
    ```
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
        *,
        provider: Annotated[
            SQLiteProvider | None,
            Doc(
                """
                A pre-built `SQLiteProvider`. When set, the adapter
                borrows the provider's connection and does not manage
                its lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `SQLiteProvider` when `provider` is not set. Resolves
                the path from `SQLITE_PATH` by default.
                """,
            ),
        ] = "SQLITE_",
        table_name: Annotated[
            str, Doc("The table name to store the locks.")
        ] = "locks",
    ) -> None:
        """Initialize the lock backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise ValueError(msg)

        if provider is None:
            self._provider = SQLiteProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._table_name = table_name
        self._acquire_sql = self._SQL_ACQUIRE_OR_EXTEND.format(
            table_name=table_name
        )
        self._release_sql = self._SQL_RELEASE.format(table_name=table_name)
        self._locked_sql = self._SQL_LOCKED.format(table_name=table_name)
        self._owned_sql = self._SQL_OWNED.format(table_name=table_name)
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def provider(self) -> SQLiteProvider:
        """The bound `SQLiteProvider`."""
        return self._provider

    def _rebind_provider(self, provider: SQLiteProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the adapter and its provider when owned."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        async with self._provider.connection_lock:
            await self._provider.client.execute(
                self._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(
                    table_name=self._table_name
                ),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        async with self._provider.connection_lock:
            await self._provider.client.execute(
                self._SQL_RELEASE_ALL_EXPIRED.format(
                    table_name=self._table_name
                ),
            )
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a lock, returning the fencing token or `None`.

        Runs the read-modify-write inside a `BEGIN IMMEDIATE` transaction so
        the fence high-water update is serialized against concurrent writers.
        """
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                async with conn.execute(
                    self._acquire_sql, (name, token, ceil(duration))
                ) as cursor:
                    result = await cursor.fetchone()
                await conn.execute("COMMIT;")
            except BaseException:
                await conn.execute("ROLLBACK;")
                raise
        return int(result[0]) if result is not None else None

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(self._release_sql, (name, token)) as cursor,
        ):
            result = await cursor.fetchone()
        return result is not None

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(self._locked_sql, (name,)) as cursor,
        ):
            result = await cursor.fetchone()
        return result is not None

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(self._owned_sql, (name, token)) as cursor,
        ):
            result = await cursor.fetchone()
        return result is not None


class SQLiteScheduleAdapter(ScheduleBackend):
    """SQLite Schedule Adapter.

    Borrows the connection and a shared lock from a `SQLiteProvider` and
    implements the `ScheduleBackend` protocol for durable distributed cron on a
    single host. Pass an explicit `provider=` to share a connection with other
    components, or rely on the default `env_prefix=` to build one from
    environment variables.

    The `last_fired` epoch is stored as a `REAL` column on a row keyed by
    `name`, and the claim decision runs in a single UPSERT gated by a `WHERE`
    clause. The cursor's `rowcount` tells whether this call performed the
    write, so the compare-and-set is atomic across processes sharing the file.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.coordination import Coordination
    from grelmicro.providers.sqlite import SQLiteProvider

    sqlite = SQLiteProvider("app.db")
    micro = Grelmicro(uses=[sqlite, Coordination(schedule=sqlite)])
    ```
    """

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
                CREATE TABLE IF NOT EXISTS {table_name} (
                    name TEXT PRIMARY KEY,
                    last_fired REAL NOT NULL
                );
                """

    _SQL_CLAIM = """
                INSERT INTO {table_name} (name, last_fired)
                VALUES (?, ?)
                ON CONFLICT (name) DO UPDATE
                SET last_fired = excluded.last_fired
                WHERE last_fired < excluded.last_fired;
                """

    _SQL_LAST_FIRED = """
        SELECT last_fired FROM {table_name} WHERE name = ?;
        """

    def __init__(
        self,
        *,
        provider: Annotated[
            SQLiteProvider | None,
            Doc(
                """
                A pre-built `SQLiteProvider`. When set, the adapter
                borrows the provider's connection and does not manage
                its lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `SQLiteProvider` when `provider` is not set. Resolves
                the path from `SQLITE_PATH` by default.
                """,
            ),
        ] = "SQLITE_",
        table_name: Annotated[
            str, Doc("The table name to store the schedules.")
        ] = "schedules",
    ) -> None:
        """Initialize the schedule backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise ValueError(msg)

        if provider is None:
            self._provider = SQLiteProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._table_name = table_name
        self._claim_sql = self._SQL_CLAIM.format(table_name=table_name)
        self._last_fired_sql = self._SQL_LAST_FIRED.format(
            table_name=table_name
        )
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def provider(self) -> SQLiteProvider:
        """The bound `SQLiteProvider`."""
        return self._provider

    def _rebind_provider(self, provider: SQLiteProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the adapter and its provider when owned."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        async with self._provider.connection_lock:
            await self._provider.client.execute(
                self._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(
                    table_name=self._table_name
                ),
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def claim(self, name: str, due: float) -> bool:
        """Atomically claim the fire at `due`.

        Runs the gated UPSERT inside a `BEGIN IMMEDIATE` transaction so the
        compare-and-set is serialized against concurrent writers. The provider's
        lock serializes the single connection within the process, and the
        transaction's write lock serializes across processes sharing the file.
        An insert or a successful update changes one row (won), the gated update
        changes none (lost).
        """
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                async with conn.execute(self._claim_sql, (name, due)) as cursor:
                    changes = cursor.rowcount
                await conn.execute("COMMIT;")
            except BaseException:
                await conn.execute("ROLLBACK;")
                raise
        return changes == 1

    async def last_fired(self, name: str) -> float | None:
        """Return the stored `last_fired` epoch, or `None`."""
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(self._last_fired_sql, (name,)) as cursor,
        ):
            result = await cursor.fetchone()
        return float(result[0]) if result is not None else None
