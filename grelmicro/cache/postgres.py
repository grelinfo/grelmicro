"""Postgres Cache Adapter."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.cache._protocol import CacheBackend
from grelmicro.providers.postgres import PostgresProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType


class PostgresCacheAdapter(CacheBackend):
    """Postgres cache storage backend.

    Wraps a `PostgresProvider` and implements the cache protocol:
    `get`, `set` (with per-entry TTL via `expires_at`), `delete`,
    batch operations, tag-based invalidation, and a prefix-scoped
    `clear`. Entries live in a single table keyed on `key` with
    `value BYTEA` and `expires_at TIMESTAMPTZ`.

    Tags live in a companion table that maps each key to its tags. A
    foreign key with `ON DELETE CASCADE` removes a key's tag rows
    whenever the key row goes away, so deleting by tag, by key, or by
    janitor never leaves an orphan tag row behind.

    Pass an explicit `provider=` to share a pool with other
    components, or rely on the default `env_prefix=` to build one
    from environment variables.

    Set `cleanup_interval=` to enable a background janitor that
    deletes expired rows. Off by default. Lazy expiry on `get`
    keeps reads correct, the janitor only reclaims storage.

    Example:
    ```python
    from grelmicro.cache import Cache
    from grelmicro.providers.postgres import PostgresProvider

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    cache = Cache(postgres)
    ```

    Read more in the [Cache](../cache.md) docs.
    """

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            key TEXT PRIMARY KEY,
            value BYTEA NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
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
        "SELECT value FROM {table_name} WHERE key = $1 AND expires_at > NOW();"
    )

    _SQL_GET_MANY = (
        "SELECT key, value FROM {table_name} "
        "WHERE key = ANY($1) AND expires_at > NOW();"
    )

    _SQL_SET = (
        "INSERT INTO {table_name} (key, value, expires_at) "
        "VALUES ($1, $2, NOW() + make_interval(secs => $3)) "
        "ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at;"
    )

    _SQL_DELETE_TAGS_OF_KEY = "DELETE FROM {table_name}_tags WHERE key = $1;"

    _SQL_INSERT_TAG = (
        "INSERT INTO {table_name}_tags (key, tag) VALUES ($1, $2) "
        "ON CONFLICT (key, tag) DO NOTHING;"
    )

    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = $1;"

    _SQL_DELETE_MANY = "DELETE FROM {table_name} WHERE key = ANY($1);"

    _SQL_DELETE_BY_TAGS = (
        "DELETE FROM {table_name} WHERE key IN "
        "(SELECT key FROM {table_name}_tags WHERE tag = ANY($1));"
    )

    _SQL_CLEAR_PREFIX = "DELETE FROM {table_name} WHERE key LIKE $1;"

    _SQL_CLEAR_ALL = "DELETE FROM {table_name};"

    _SQL_JANITOR = (
        "DELETE FROM {table_name} WHERE expires_at < NOW() - INTERVAL '1 hour';"
    )

    def __init__(
        self,
        *,
        provider: Annotated[
            PostgresProvider | None,
            Doc(
                """
                A pre-built `PostgresProvider`. When set, the adapter
                borrows the provider's pool and does not manage its
                lifecycle.
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `PostgresProvider` when `provider` is not set. Defaults
                to `POSTGRES_`. Use a custom prefix to split pools.
                """,
            ),
        ] = "POSTGRES_",
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
        """Initialize the Postgres cache backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise ValueError(msg)

        if cleanup_interval is not None and cleanup_interval <= 0:
            msg = f"cleanup_interval must be positive, got {cleanup_interval!r}"
            raise ValueError(msg)

        if provider is None:
            self._provider = PostgresProvider(env_prefix=env_prefix)
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
        self._get_many_sql = self._SQL_GET_MANY.format(table_name=table_name)
        self._set_sql = self._SQL_SET.format(table_name=table_name)
        self._delete_tags_of_key_sql = self._SQL_DELETE_TAGS_OF_KEY.format(
            table_name=table_name
        )
        self._insert_tag_sql = self._SQL_INSERT_TAG.format(
            table_name=table_name
        )
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
        self._delete_many_sql = self._SQL_DELETE_MANY.format(
            table_name=table_name
        )
        self._delete_by_tags_sql = self._SQL_DELETE_BY_TAGS.format(
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
    def provider(self) -> PostgresProvider:
        """The bound `PostgresProvider`."""
        return self._provider

    def _rebind_provider(self, provider: PostgresProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the cache connection, install the schema, start the janitor."""
        self._loop = asyncio.get_running_loop()
        if self._owns_provider:
            await self._provider.__aenter__()
        if self._auto_migrate:
            await self._provider.client.execute(
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
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def get(self, *, key: str) -> bytes | None:
        """Get raw bytes by key.

        Returns None if the key is missing or expired.
        """
        row = await self._provider.client.fetchrow(
            self._get_sql, f"{self._key_prefix}{key}"
        )
        return row["value"] if row is not None else None

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
        async with self._provider.client.acquire() as conn, conn.transaction():
            await conn.execute(self._set_sql, full_key, value, float(ttl))
            await conn.execute(self._delete_tags_of_key_sql, full_key)
            if tags:
                await conn.executemany(
                    self._insert_tag_sql,
                    [(full_key, tag) for tag in tags],
                )

    async def get_many(self, *, keys: Sequence[str]) -> dict[str, bytes]:
        """Get raw bytes for many keys, returning only found entries."""
        full_keys = [f"{self._key_prefix}{key}" for key in keys]
        if not full_keys:
            return {}
        rows = await self._provider.client.fetch(self._get_many_sql, full_keys)
        plen = len(self._key_prefix)
        return {row["key"][plen:]: row["value"] for row in rows}

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
        ttl_f = float(ttl)
        async with self._provider.client.acquire() as conn, conn.transaction():
            for key, value in items.items():
                full_key = f"{self._key_prefix}{key}"
                await conn.execute(self._set_sql, full_key, value, ttl_f)
                await conn.execute(self._delete_tags_of_key_sql, full_key)
                if tags:
                    await conn.executemany(
                        self._insert_tag_sql,
                        [(full_key, tag) for tag in tags],
                    )

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent).

        The cascade removes the key's tag rows.
        """
        await self._provider.client.execute(
            self._delete_sql, f"{self._key_prefix}{key}"
        )

    async def delete_many(self, *, keys: Sequence[str]) -> None:
        """Delete many keys. The cascade removes their tag rows."""
        full_keys = [f"{self._key_prefix}{key}" for key in keys]
        if not full_keys:
            return
        await self._provider.client.execute(self._delete_many_sql, full_keys)

    async def delete_tags(self, *, tags: Sequence[str]) -> None:
        """Delete every key associated with any of the given tags.

        One statement deletes the matching value rows, and the cascade
        cleans their tag rows.
        """
        if not tags:
            return
        await self._provider.client.execute(
            self._delete_by_tags_sql, list(tags)
        )

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Falls back to a full table delete when no prefix is set. The
        cascade cleans the matching tag rows.
        """
        if self._key_prefix:
            await self._provider.client.execute(
                self._clear_prefix_sql,
                f"{_escape_like(self._key_prefix)}%",
            )
        else:
            await self._provider.client.execute(self._clear_all_sql)

    async def _janitor_loop(self) -> None:
        """Periodically delete rows expired for more than one hour."""
        interval = self._cleanup_interval or 0
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self._provider.client.execute(self._janitor_sql)


def _escape_like(value: str) -> str:
    r"""Escape `%`, `_`, and `\` for a SQL `LIKE` pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
