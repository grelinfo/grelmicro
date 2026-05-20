"""Postgres Cache Adapter."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.providers.postgres import PostgresProvider

if TYPE_CHECKING:
    from types import TracebackType


class PostgresCacheAdapter:
    """Postgres cache storage backend.

    Wraps a `PostgresProvider` and implements the cache protocol:
    `get`, `set` (with per-entry TTL via `expires_at`), `delete`,
    and a prefix-scoped `clear`. Entries live in a single table
    keyed on `key` with `value BYTEA` and `expires_at TIMESTAMPTZ`.

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
    """

    _SQL_GET = (
        "SELECT value FROM {table_name} WHERE key = $1 AND expires_at > NOW();"
    )

    _SQL_SET = (
        "INSERT INTO {table_name} (key, value, expires_at) "
        "VALUES ($1, $2, NOW() + make_interval(secs => $3)) "
        "ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at;"
    )

    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = $1;"

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
        self._set_sql = self._SQL_SET.format(table_name=table_name)
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
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

    async def set(self, *, key: str, value: bytes, ttl: float) -> None:
        """Store raw bytes with a TTL in seconds."""
        await self._provider.client.execute(
            self._set_sql,
            f"{self._key_prefix}{key}",
            value,
            float(ttl),
        )

    async def delete(self, *, key: str) -> None:
        """Delete a key (no-op if absent)."""
        await self._provider.client.execute(
            self._delete_sql, f"{self._key_prefix}{key}"
        )

    async def clear(self) -> None:
        """Remove all entries matching the configured prefix.

        Falls back to a full table delete when no prefix is set.
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
