"""PostgreSQL Lock Backend."""

from types import TracebackType
from typing import Annotated, Self

from asyncpg import Pool, create_pool
from pydantic import PostgresDsn
from typing_extensions import Doc

from grelmicro.abc.lockbackend import LockBackend
from grelmicro.backends.registry import loaded_backends
from grelmicro.errors import OutOfContextError


class PostgresLockBackend(LockBackend):
    """PostgreSQL Lock Backend."""

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
        url: Annotated[PostgresDsn | str, Doc("The Postgres database URL.")],
        *,
        auto_register: Annotated[
            bool,
            Doc("Automatically register the lock backend in the backend registry."),
        ] = True,
        table_name: Annotated[str, Doc("The table name to store the locks.")] = "locks",
    ) -> None:
        """Initialize the lock backend."""
        if not table_name.isidentifier():
            msg = f"Table name '{table_name}' is not a valid identifier."
            raise ValueError(msg)

        self._url = url
        self._table_name = table_name
        self._acquire_sql = self._SQL_ACQUIRE_OR_EXTEND.format(table_name=table_name)
        self._release_sql = self._SQL_RELEASE.format(table_name=table_name)
        self._pool: Pool | None = None
        if auto_register:
            loaded_backends["lock"] = self

    async def __aenter__(self) -> Self:
        """Enter the lock backend."""
        self._pool = await create_pool(str(self._url))
        await self._pool.execute(
            self._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(table_name=self._table_name),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the lock backend."""
        if self._pool:
            await self._pool.execute(
                self._SQL_RELEASE_ALL_EXPIRED.format(table_name=self._table_name),
            )
            await self._pool.close()

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire a lock."""
        if not self._pool:
            raise OutOfContextError(self, "acquire")

        return bool(await self._pool.fetchval(self._acquire_sql, name, token, duration))

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        if not self._pool:
            raise OutOfContextError(self, "release")
        return bool(await self._pool.fetchval(self._release_sql, name, token))

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        if not self._pool:
            raise OutOfContextError(self, "locked")
        return bool(
            await self._pool.fetchval(
                self._SQL_LOCKED.format(table_name=self._table_name),
                name,
            ),
        )

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        if not self._pool:
            raise OutOfContextError(self, "owned")
        return bool(
            await self._pool.fetchval(
                self._SQL_OWNED.format(table_name=self._table_name),
                name,
                token,
            ),
        )
