"""SQLite Cache Adapter."""

from __future__ import annotations

import asyncio
import contextlib
import re
from time import time
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.providers.sqlite import SQLiteProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType


class SQLiteCacheAdapter:
    """SQLite cache storage backend.

    Borrows the connection and a shared lock from a `SQLiteProvider`
    and implements the cache protocol: `get`, `set` (with per-entry TTL
    via `expires_at`), `delete`, batch operations, tag-based
    invalidation, and a prefix-scoped `clear`. Entries live in a single
    table keyed on `key` with `value BLOB` and `expires_at REAL` (an
    epoch in seconds).

    Tags live in a companion table that maps each key to its tags. A
    foreign key with `ON DELETE CASCADE` removes a key's tag rows
    whenever the key row goes away, so deleting by tag, by key, or by
    janitor never leaves an orphan tag row behind.

    Reads and writes run inside a `BEGIN IMMEDIATE` transaction. The
    provider's lock serializes the single connection within the process,
    and the transaction's write lock serializes across processes sharing
    the same file. State survives process restarts.

    Pass an explicit `provider=` to share a connection with other
    components, or rely on the default `env_prefix=` to build one from
    environment variables.

    Set `cleanup_interval=` to enable a background janitor that deletes
    expired rows. Off by default. Lazy expiry on `get` keeps reads
    correct, the janitor only reclaims storage.

    Example:
    ```python
    from grelmicro.cache import Cache
    from grelmicro.providers.sqlite import SQLiteProvider

    sqlite = SQLiteProvider("app.db")
    cache = Cache(sqlite)
    ```

    Read more in the [Cache](../cache.md) docs.
    """

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            key TEXT PRIMARY KEY,
            value BLOB NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS {table_name}_expires_at_idx
            ON {table_name} (expires_at);
        CREATE TABLE IF NOT EXISTS {table_name}_tags (
            key TEXT REFERENCES {table_name} (key) ON DELETE CASCADE,
            tag TEXT,
            PRIMARY KEY (key, tag)
        );
        CREATE INDEX IF NOT EXISTS {table_name}_tags_tag_idx
            ON {table_name}_tags (tag);
    """

    _SQL_GET = (
        "SELECT value FROM {table_name} WHERE key = ? AND expires_at > ?;"
    )

    _SQL_SET = """
        INSERT INTO {table_name} (key, value, expires_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE
            SET value = excluded.value, expires_at = excluded.expires_at;
    """

    _SQL_DELETE_TAGS_OF_KEY = "DELETE FROM {table_name}_tags WHERE key = ?;"

    _SQL_INSERT_TAG = (
        "INSERT INTO {table_name}_tags (key, tag) VALUES (?, ?) "
        "ON CONFLICT (key, tag) DO NOTHING;"
    )

    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = ?;"

    _SQL_DELETE_BY_TAG = (
        "DELETE FROM {table_name} WHERE key IN "
        "(SELECT key FROM {table_name}_tags WHERE tag = ?);"
    )

    _SQL_CLEAR_PREFIX = "DELETE FROM {table_name} WHERE key LIKE ? ESCAPE '\\';"

    _SQL_CLEAR_ALL = "DELETE FROM {table_name};"

    _SQL_JANITOR = "DELETE FROM {table_name} WHERE expires_at < ?;"

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
        prefix: Annotated[
            str,
            Doc("Prefix prepended to every key (cache namespace)."),
        ] = "",
        table_name: Annotated[
            str,
            Doc(
                """
                Table that stores cache entries. Auto-created on first
                connect (set `auto_migrate=False` to opt out).
                """,
            ),
        ] = "grelmicro_cache",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the adapter creates the table
                on `__aenter__`. Set to False when the schema is
                managed by your own migration tool.
                """,
            ),
        ] = True,
        cleanup_interval: Annotated[
            float | None,
            Doc(
                """
                Period in seconds between janitor runs that delete
                rows expired for more than one hour. Default `None`
                disables the janitor: lazy expiry on `get` still
                works, only storage reclamation is skipped.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the SQLite cache backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise ValueError(msg)

        if cleanup_interval is not None and cleanup_interval <= 0:
            msg = f"cleanup_interval must be positive, got {cleanup_interval!r}"
            raise ValueError(msg)

        if provider is None:
            self._provider = SQLiteProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._key_prefix = prefix
        self._table_name = table_name
        self._auto_migrate = auto_migrate
        self._cleanup_interval = cleanup_interval
        self._get_sql = self._SQL_GET.format(table_name=table_name)
        self._set_sql = self._SQL_SET.format(table_name=table_name)
        self._delete_tags_of_key_sql = self._SQL_DELETE_TAGS_OF_KEY.format(
            table_name=table_name
        )
        self._insert_tag_sql = self._SQL_INSERT_TAG.format(
            table_name=table_name
        )
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
        self._delete_by_tag_sql = self._SQL_DELETE_BY_TAG.format(
            table_name=table_name
        )
        self._clear_prefix_sql = self._SQL_CLEAR_PREFIX.format(
            table_name=table_name
        )
        self._clear_all_sql = self._SQL_CLEAR_ALL.format(table_name=table_name)
        self._janitor_sql = self._SQL_JANITOR.format(table_name=table_name)
        self._janitor_task: asyncio.Task[None] | None = None
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
        """Open the cache connection, install the schema, start the janitor."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        if self._auto_migrate:
            await self._provider.client.execute("PRAGMA foreign_keys=ON;")
            await self._provider.client.executescript(
                self._SQL_CREATE_TABLE.format(table_name=self._table_name)
            )
        if self._cleanup_interval is not None:
            self._janitor_task = asyncio.create_task(self._janitor_loop())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the janitor and close the provider when owned."""
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._janitor_task
            self._janitor_task = None
        self._loop = None
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(
                self._get_sql, (f"{self._key_prefix}{key}", time())
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return row[0] if row is not None else None

    async def set(
        self,
        *,
        key: str,
        value: bytes,
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Store raw bytes with a TTL in seconds and optional tags.

        The value upsert and the tag rows commit in one transaction.
        """
        full_key = f"{self._key_prefix}{key}"
        expires_at = time() + float(ttl)
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                await conn.execute(self._set_sql, (full_key, value, expires_at))
                await conn.execute(self._delete_tags_of_key_sql, (full_key,))
                if tags:
                    await conn.executemany(
                        self._insert_tag_sql,
                        [(full_key, tag) for tag in tags],
                    )
                await conn.execute("COMMIT;")
            except BaseException:
                await conn.execute("ROLLBACK;")
                raise

    async def get_many(self, *, keys: Sequence[str]) -> dict[str, bytes]:
        """Get raw bytes for many keys, returning only found entries."""
        if not keys:
            return {}
        full_keys = [f"{self._key_prefix}{key}" for key in keys]
        placeholders = ",".join("?" * len(full_keys))
        table = self._table_name
        sql = f"SELECT key, value FROM {table} WHERE key IN ({placeholders}) AND expires_at > ?;"  # noqa: S608
        conn = self._provider.client
        async with (
            self._provider.connection_lock,
            conn.execute(sql, (*full_keys, time())) as cursor,
        ):
            rows = await cursor.fetchall()
        plen = len(self._key_prefix)
        return {row[0][plen:]: row[1] for row in rows}

    async def set_many(
        self,
        *,
        items: Mapping[str, bytes],
        ttl: float,
        tags: Sequence[str] = (),
    ) -> None:
        """Store many keys with one TTL and optional tags.

        Every value upsert and its tag rows commit in one transaction.
        """
        if not items:
            return
        expires_at = time() + float(ttl)
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                for key, value in items.items():
                    full_key = f"{self._key_prefix}{key}"
                    await conn.execute(
                        self._set_sql, (full_key, value, expires_at)
                    )
                    await conn.execute(
                        self._delete_tags_of_key_sql, (full_key,)
                    )
                    if tags:
                        await conn.executemany(
                            self._insert_tag_sql,
                            [(full_key, tag) for tag in tags],
                        )
                await conn.execute("COMMIT;")
            except BaseException:
                await conn.execute("ROLLBACK;")
                raise

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent).

        The cascade removes the key's tag rows.
        """
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute(self._delete_sql, (f"{self._key_prefix}{key}",))

    async def delete_many(self, *, keys: Sequence[str]) -> None:
        """Delete many keys. The cascade removes their tag rows."""
        if not keys:
            return
        full_keys = [f"{self._key_prefix}{key}" for key in keys]
        placeholders = ",".join("?" * len(full_keys))
        sql = f"DELETE FROM {self._table_name} WHERE key IN ({placeholders});"  # noqa: S608
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute(sql, full_keys)

    async def delete_tags(self, *, tags: Sequence[str]) -> None:
        """Delete every key associated with any of the given tags.

        The cascade cleans the matching tag rows.
        """
        if not tags:
            return
        conn = self._provider.client
        async with self._provider.connection_lock:
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                for tag in tags:
                    await conn.execute(self._delete_by_tag_sql, (tag,))
                await conn.execute("COMMIT;")
            except BaseException:
                await conn.execute("ROLLBACK;")
                raise

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Falls back to a full table delete when no prefix is set. The
        cascade cleans the matching tag rows.
        """
        conn = self._provider.client
        async with self._provider.connection_lock:
            if self._key_prefix:
                await conn.execute(
                    self._clear_prefix_sql,
                    (f"{_escape_like(self._key_prefix)}%",),
                )
            else:
                await conn.execute(self._clear_all_sql)

    async def _janitor_loop(self) -> None:
        """Periodically delete rows expired for more than one hour."""
        interval = self._cleanup_interval or 0
        conn = self._provider.client
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                async with self._provider.connection_lock:
                    await conn.execute(self._janitor_sql, (time() - 3600,))


def _escape_like(value: str) -> str:
    r"""Escape `%`, `_`, and `\` for a SQL `LIKE` pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
