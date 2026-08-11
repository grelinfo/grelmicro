"""SQLite coordination adapters."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from math import ceil
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.coordination._protocol import (
    LockBackend,
    ReadWriteLockBackend,
    ReadWriteLockState,
    ScheduleBackend,
    WriteGrant,
)
from grelmicro.providers.sqlite import SQLiteProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from aiosqlite import Connection

    from grelmicro.types import BackendScope


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

    scope: ClassVar[BackendScope] = "host"
    """State is shared by the processes that open the same file."""

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


class SQLiteReadWriteLockAdapter(ReadWriteLockBackend):
    """SQLite Read-Write Lock Adapter.

    Borrows the connection and a shared lock from a `SQLiteProvider` and
    implements the `ReadWriteLockBackend` protocol on a single host. One row
    per lock holds the writer token, the writer's expiry, and the generation
    counter. A side table holds one row per reader lease and one per writer
    intent.

    Every operation runs inside a `BEGIN IMMEDIATE` transaction, so the reap,
    the decision, and the write apply as one step. The provider's lock
    serializes the single connection within the process, and the
    transaction's write lock serializes across processes sharing the file.

    Lease durations are rounded up to whole seconds, the resolution SQLite
    date functions work at.
    """

    scope: ClassVar[BackendScope] = "host"
    """State is shared by the processes that open the same file."""

    _SQL_CREATE_TABLES = (
        """
        CREATE TABLE IF NOT EXISTS {table_name} (
            name TEXT PRIMARY KEY,
            writer TEXT,
            writer_expire_at TEXT,
            generation INTEGER NOT NULL DEFAULT 0
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS {table_name}_holders (
            name TEXT NOT NULL,
            token TEXT NOT NULL,
            kind TEXT NOT NULL,
            expire_at TEXT NOT NULL,
            PRIMARY KEY (name, token, kind)
        );
        """,
    )

    _SQL_REAP = """
        DELETE FROM {table_name}_holders
        WHERE name = ? AND expire_at < datetime('now');
    """

    _SQL_ENSURE_ROW = """
        INSERT INTO {table_name} (name) VALUES (?)
        ON CONFLICT (name) DO NOTHING;
    """

    _SQL_SELECT_LOCK = """
        SELECT writer, writer_expire_at, generation,
               (writer IS NOT NULL AND writer_expire_at >= datetime('now'))
        FROM {table_name} WHERE name = ?;
    """

    _SQL_COUNT_HOLDERS = """
        SELECT count(*) FROM {table_name}_holders
        WHERE name = ? AND kind = ? AND expire_at >= datetime('now');
    """

    _SQL_HOLDER_EXISTS = """
        SELECT 1 FROM {table_name}_holders
        WHERE name = ? AND token = ? AND kind = ?
          AND expire_at >= datetime('now');
    """

    _SQL_UPSERT_HOLDER = """
        INSERT INTO {table_name}_holders (name, token, kind, expire_at)
        VALUES (?, ?, ?, datetime('now', '+' || ? || ' seconds'))
        ON CONFLICT (name, token, kind)
        DO UPDATE SET expire_at = excluded.expire_at;
    """

    _SQL_DELETE_HOLDER = """
        DELETE FROM {table_name}_holders
        WHERE name = ? AND token = ? AND kind = ?
          AND expire_at >= datetime('now')
        RETURNING 1;
    """

    _SQL_SET_WRITER = """
        UPDATE {table_name}
        SET writer = ?,
            writer_expire_at = datetime('now', '+' || ? || ' seconds'),
            generation = generation + 1
        WHERE name = ?
        RETURNING generation;
    """

    _SQL_RENEW_WRITER = """
        UPDATE {table_name}
        SET writer_expire_at = datetime('now', '+' || ? || ' seconds')
        WHERE name = ?;
    """

    _SQL_CLEAR_WRITER = """
        UPDATE {table_name}
        SET writer = NULL, writer_expire_at = NULL
        WHERE name = ? AND writer = ? AND writer_expire_at >= datetime('now')
        RETURNING generation;
    """

    def __init__(
        self,
        *,
        provider: Annotated[
            SQLiteProvider | None,
            Doc(
                """
                A pre-built `SQLiteProvider`. When set, the adapter borrows
                the provider's connection and does not manage its lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `SQLiteProvider` when `provider` is not set. Resolves the
                path from `SQLITE_PATH` by default.
                """,
            ),
        ] = "SQLITE_",
        table_name: Annotated[
            str, Doc("The table name to store the read-write locks.")
        ] = "rwlocks",
    ) -> None:
        """Initialize the read-write lock backend."""
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
        self._sql = {
            key: template.format(table_name=table_name)
            for key, template in {
                "reap": self._SQL_REAP,
                "ensure_row": self._SQL_ENSURE_ROW,
                "select_lock": self._SQL_SELECT_LOCK,
                "count_holders": self._SQL_COUNT_HOLDERS,
                "holder_exists": self._SQL_HOLDER_EXISTS,
                "upsert_holder": self._SQL_UPSERT_HOLDER,
                "delete_holder": self._SQL_DELETE_HOLDER,
                "set_writer": self._SQL_SET_WRITER,
                "renew_writer": self._SQL_RENEW_WRITER,
                "clear_writer": self._SQL_CLEAR_WRITER,
            }.items()
        }
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
            for sql in self._SQL_CREATE_TABLES:
                await self._provider.client.execute(
                    sql.format(table_name=self._table_name)
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

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Connection]:
        """Run a body inside `BEGIN IMMEDIATE` on the shared connection."""
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK;")
                raise
            await conn.execute("COMMIT;")

    async def _fetch(
        self, conn: Connection, key: str, params: tuple[object, ...]
    ) -> Any:  # noqa: ANN401
        """Run a statement and return its first row."""
        async with conn.execute(self._sql[key], params) as cursor:
            return await cursor.fetchone()

    async def acquire_read(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a read lease, returning the generation or `None`."""
        seconds = ceil(duration)
        async with self._transaction() as conn:
            await conn.execute(self._sql["reap"], (name,))
            await conn.execute(self._sql["ensure_row"], (name,))
            row = await self._fetch(conn, "select_lock", (name,))
            assert row is not None  # noqa: S101
            generation, writing = int(row[2]), bool(row[3])
            held = await self._fetch(conn, "holder_exists", (name, token, "r"))
            if held is None:
                if writing:
                    return None
                intents = await self._fetch(conn, "count_holders", (name, "i"))
                assert intents is not None  # noqa: S101
                if int(intents[0]) > 0:
                    return None
            await conn.execute(
                self._sql["upsert_holder"], (name, token, "r", seconds)
            )
            return generation

    async def acquire_write(
        self, *, name: str, token: str, duration: float, intent: bool = True
    ) -> WriteGrant | None:
        """Acquire the write lease, returning the grant or `None`."""
        seconds = ceil(duration)
        async with self._transaction() as conn:
            await conn.execute(self._sql["reap"], (name,))
            await conn.execute(self._sql["ensure_row"], (name,))
            row = await self._fetch(conn, "select_lock", (name,))
            assert row is not None  # noqa: S101
            writer, generation, writing = row[0], int(row[2]), bool(row[3])
            if writing and writer == token:
                await conn.execute(self._sql["renew_writer"], (seconds, name))
                return WriteGrant(fencing_token=generation, poisoned=False)
            readers = await self._fetch(conn, "count_holders", (name, "r"))
            assert readers is not None  # noqa: S101
            if writing or int(readers[0]) > 0:
                if intent:
                    await conn.execute(
                        self._sql["upsert_holder"],
                        (name, token, "i", seconds),
                    )
                return None
            await conn.execute(
                f"DELETE FROM {self._table_name}_holders"  # noqa: S608
                " WHERE name = ? AND token = ? AND kind = 'i';",
                (name, token),
            )
            granted = await self._fetch(
                conn, "set_writer", (token, seconds, name)
            )
            assert granted is not None  # noqa: S101
            return WriteGrant(
                fencing_token=int(granted[0]), poisoned=writer is not None
            )

    async def release_read(self, *, name: str, token: str) -> bool:
        """Drop a read lease."""
        async with self._transaction() as conn:
            row = await self._fetch(conn, "delete_holder", (name, token, "r"))
            return row is not None

    async def release_write(self, *, name: str, token: str) -> bool:
        """Drop the write lease, leaving the lock clean."""
        async with self._transaction() as conn:
            row = await self._fetch(conn, "clear_writer", (name, token))
            return row is not None

    async def cancel_intent(self, *, name: str, token: str) -> bool:
        """Withdraw a writer intent."""
        async with self._transaction() as conn:
            row = await self._fetch(conn, "delete_holder", (name, token, "i"))
            return row is not None

    async def downgrade(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Turn a held write lease into a read lease."""
        seconds = ceil(duration)
        async with self._transaction() as conn:
            row = await self._fetch(conn, "clear_writer", (name, token))
            if row is None:
                return None
            await conn.execute(
                self._sql["upsert_holder"], (name, token, "r", seconds)
            )
            return int(row[0])

    async def state(self, *, name: str) -> ReadWriteLockState:
        """Return a point-in-time view of the lock."""
        async with self._transaction() as conn:
            await conn.execute(self._sql["reap"], (name,))
            row = await self._fetch(conn, "select_lock", (name,))
            if row is None:
                return ReadWriteLockState(
                    generation=0,
                    writing=False,
                    readers=0,
                    waiting_writers=0,
                )
            readers = await self._fetch(conn, "count_holders", (name, "r"))
            intents = await self._fetch(conn, "count_holders", (name, "i"))
            assert readers is not None  # noqa: S101
            assert intents is not None  # noqa: S101
            return ReadWriteLockState(
                generation=int(row[2]),
                writing=bool(row[3]),
                readers=int(readers[0]),
                waiting_writers=int(intents[0]),
            )

    async def owned_read(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds a live read lease."""
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(
                self._sql["holder_exists"], (name, token, "r")
            ) as cursor,
        ):
            return await cursor.fetchone() is not None

    async def owned_write(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds the live write lease."""
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(self._sql["select_lock"], (name,)) as cursor,
        ):
            row = await cursor.fetchone()
        return row is not None and row[0] == token and bool(row[3])


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

    scope: ClassVar[BackendScope] = "host"
    """State is shared by the processes that open the same file."""

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
