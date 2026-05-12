"""PostgreSQL Synchronization Adapter."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro.providers.postgres import PostgresProvider
from grelmicro.sync.abc import SyncBackend

if TYPE_CHECKING:
    from types import TracebackType


class PostgresSyncAdapter(SyncBackend):
    """PostgreSQL Synchronization Adapter.

    Wraps a `PostgresProvider` and implements the `SyncBackend` protocol
    for distributed locks. Pass an explicit `provider=` to share a pool
    with other components, or rely on the default `env_prefix=` to build
    one from environment variables.
    """

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
                CREATE TABLE IF NOT EXISTS {table_name} (
                    name TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    expire_at TIMESTAMP NOT NULL
                );
                """

    _SQL_ACQUIRE_OR_EXTEND = """
                INSERT INTO {table_name} (name, token, expire_at)
                VALUES ($1, $2, NOW() + make_interval(secs => $3))
                ON CONFLICT (name) DO UPDATE
                SET token = EXCLUDED.token, expire_at = EXCLUDED.expire_at
                WHERE {table_name}.token = EXCLUDED.token OR {table_name}.expire_at < NOW()
                RETURNING 1;
                """

    _SQL_RELEASE = """
            DELETE FROM {table_name}
            WHERE name = $1 AND token = $2 AND expire_at >= NOW()
            RETURNING 1;
            """

    _SQL_RELEASE_ALL_EXPIRED = """
        DELETE FROM {table_name}
        WHERE expire_at < NOW();
        """

    _SQL_LOCKED = """
        SELECT 1 FROM {table_name}
        WHERE name = $1 AND expire_at >= NOW();
        """

    _SQL_OWNED = """
        SELECT 1 FROM {table_name}
        WHERE name = $1 AND token = $2 AND expire_at >= NOW();
        """

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
        table_name: Annotated[
            str, Doc("The table name to store the locks.")
        ] = "locks",
    ) -> None:
        """Initialize the adapter."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise ValueError(msg)

        if provider is None:
            self._provider = PostgresProvider(env_prefix=env_prefix)
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

    @property
    def provider(self) -> PostgresProvider:
        """The bound `PostgresProvider`."""
        return self._provider

    def _rebind_provider(self, provider: PostgresProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the adapter and its provider when owned."""
        if self._owns_provider:
            await self._provider.__aenter__()
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
        await self._provider.client.execute(
            self._SQL_RELEASE_ALL_EXPIRED.format(table_name=self._table_name),
        )
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire a lock."""
        return bool(
            await self._provider.client.fetchval(
                self._acquire_sql, name, token, duration
            )
        )

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        return bool(
            await self._provider.client.fetchval(self._release_sql, name, token)
        )

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        return bool(
            await self._provider.client.fetchval(self._locked_sql, name),
        )

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        return bool(
            await self._provider.client.fetchval(self._owned_sql, name, token),
        )
