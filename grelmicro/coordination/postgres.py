"""Postgres coordination adapters."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.coordination._protocol import (
    LeaderRecord,
    LockBackend,
    ReadWriteLockBackend,
    ReadWriteLockState,
    ScheduleBackend,
    WriteGrant,
)
from grelmicro.providers.postgres import PostgresProvider

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from grelmicro.types import BackendScope


class PostgresLockAdapter(LockBackend):
    """PostgreSQL Lock Adapter.

    Wraps a `PostgresProvider` and implements the `LockBackend` protocol
    for distributed locks. Pass an explicit `provider=` to share a pool
    with other components, or rely on the default `env_prefix=` to build
    one from environment variables.

    Fencing tokens live in a `fence BIGINT` column. The acquire statement
    bumps the fence on every free-to-held transition and keeps it on a
    same-holder extend, returning the value with `RETURNING fence`. Release
    clears the holder and expiry but keeps the row and its fence, so the
    fence is strictly monotonic per name across release and re-acquire cycles.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
                CREATE TABLE IF NOT EXISTS {table_name} (
                    name TEXT PRIMARY KEY,
                    token TEXT,
                    expire_at TIMESTAMP,
                    fence BIGINT NOT NULL DEFAULT 0
                );
                ALTER TABLE {table_name}
                    ADD COLUMN IF NOT EXISTS fence BIGINT NOT NULL DEFAULT 0;
                ALTER TABLE {table_name} ALTER COLUMN token DROP NOT NULL;
                ALTER TABLE {table_name} ALTER COLUMN expire_at DROP NOT NULL;
                """

    _SQL_ACQUIRE_OR_EXTEND = """
                INSERT INTO {table_name} (name, token, expire_at, fence)
                VALUES ($1, $2, NOW() + make_interval(secs => $3), 1)
                ON CONFLICT (name) DO UPDATE
                SET token = EXCLUDED.token,
                    expire_at = EXCLUDED.expire_at,
                    fence = CASE
                        WHEN {table_name}.token = EXCLUDED.token
                             AND {table_name}.expire_at >= NOW()
                        THEN {table_name}.fence
                        ELSE {table_name}.fence + 1
                    END
                WHERE {table_name}.token = EXCLUDED.token
                   OR {table_name}.token IS NULL
                   OR {table_name}.expire_at IS NULL
                   OR {table_name}.expire_at < NOW()
                RETURNING fence;
                """

    _SQL_RELEASE = """
            UPDATE {table_name}
            SET token = NULL, expire_at = NULL
            WHERE name = $1 AND token = $2 AND expire_at >= NOW()
            RETURNING 1;
            """

    _SQL_RELEASE_ALL_EXPIRED = """
        UPDATE {table_name}
        SET token = NULL, expire_at = NULL
        WHERE expire_at < NOW();
        """

    _SQL_LOCKED = """
        SELECT 1 FROM {table_name}
        WHERE name = $1 AND token IS NOT NULL AND expire_at >= NOW();
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
        """Open the adapter and its provider when owned."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        await self._migrate()
        return self

    async def _migrate(self) -> None:
        """Install the schema, guarded so replicas do not race.

        `CREATE TABLE IF NOT EXISTS` checks and creates in two steps, so
        two workers starting together can both pass the check and one then
        fails on the row type the table creates.
        """
        async with (
            self._provider.client.acquire() as conn,
            conn.transaction(),
        ):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table_name
            )
            await conn.execute(
                self._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(
                    table_name=self._table_name
                ),
            )

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

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a lock, returning the fencing token or `None`."""
        fence = await self._provider.client.fetchval(
            self._acquire_sql, name, token, duration
        )
        return int(fence) if fence is not None else None

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


_READ_WRITE_ADVISORY_NAMESPACE = 0x67726C72_776C636B
"""Advisory-lock namespace for read-write locks.

`hashtextextended` is the Postgres 64-bit text hash with a configurable
seed. A distinct seed gives read-write lock names their own 64-bit lock id
space, isolated from any other advisory lock in the same database.
"""


class PostgresReadWriteLockAdapter(ReadWriteLockBackend):
    """PostgreSQL Read-Write Lock Adapter.

    Wraps a `PostgresProvider` and implements the `ReadWriteLockBackend`
    protocol. One row per lock holds the writer token, the writer's expiry,
    and the generation counter. A side table holds one row per reader lease
    and one per writer intent.

    Every decision runs inside a PL/pgSQL function that takes
    `pg_advisory_xact_lock` for the lock name first, so the reap, the
    decision, and the write apply atomically across replicas.

    Reader leases are individual rows, so a reader that died is dropped by
    the next acquire instead of holding writers out until some shared expiry
    fires.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    _SQL_CREATE_TABLES = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            name TEXT PRIMARY KEY,
            writer TEXT,
            writer_expire_at TIMESTAMPTZ,
            generation BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS {table_name}_holders (
            name TEXT NOT NULL,
            token TEXT NOT NULL,
            kind CHAR(1) NOT NULL,
            expire_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (name, token, kind)
        );
    """

    _SQL_CREATE_FN_ACQUIRE_READ = """
        CREATE OR REPLACE FUNCTION {table_name}_acquire_read(
            p_name TEXT, p_token TEXT, p_duration DOUBLE PRECISION
        ) RETURNS BIGINT AS $$
        DECLARE
            v_now TIMESTAMPTZ := NOW();
            v_expire_at TIMESTAMPTZ;
            v_generation BIGINT;
            v_writer TEXT;
            v_writer_expire_at TIMESTAMPTZ;
            v_intents INT;
            v_held BOOLEAN;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            v_expire_at := v_now + make_interval(secs => p_duration);
            DELETE FROM {table_name}_holders
                WHERE name = p_name AND expire_at < v_now;
            INSERT INTO {table_name} (name) VALUES (p_name)
                ON CONFLICT (name) DO NOTHING;
            SELECT t.writer, t.writer_expire_at, t.generation
                INTO v_writer, v_writer_expire_at, v_generation
                FROM {table_name} t WHERE t.name = p_name;
            SELECT EXISTS(
                SELECT 1 FROM {table_name}_holders h
                    WHERE h.name = p_name AND h.token = p_token
                      AND h.kind = 'r'
            ) INTO v_held;
            IF v_held THEN
                UPDATE {table_name}_holders SET expire_at = v_expire_at
                    WHERE name = p_name AND token = p_token AND kind = 'r';
                RETURN v_generation;
            END IF;
            IF v_writer IS NOT NULL AND v_writer_expire_at > v_now THEN
                RETURN NULL;
            END IF;
            SELECT count(*) INTO v_intents FROM {table_name}_holders h
                WHERE h.name = p_name AND h.kind = 'i';
            IF v_intents > 0 THEN
                RETURN NULL;
            END IF;
            INSERT INTO {table_name}_holders (name, token, kind, expire_at)
                VALUES (p_name, p_token, 'r', v_expire_at);
            RETURN v_generation;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_ACQUIRE_WRITE = """
        CREATE OR REPLACE FUNCTION {table_name}_acquire_write(
            p_name TEXT, p_token TEXT, p_duration DOUBLE PRECISION,
            p_intent BOOLEAN
        ) RETURNS TABLE(r_fence BIGINT, r_poisoned BOOLEAN) AS $$
        DECLARE
            v_now TIMESTAMPTZ := NOW();
            v_expire_at TIMESTAMPTZ;
            v_generation BIGINT;
            v_writer TEXT;
            v_writer_expire_at TIMESTAMPTZ;
            v_readers INT;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            v_expire_at := v_now + make_interval(secs => p_duration);
            DELETE FROM {table_name}_holders
                WHERE name = p_name AND expire_at < v_now;
            INSERT INTO {table_name} (name) VALUES (p_name)
                ON CONFLICT (name) DO NOTHING;
            SELECT t.writer, t.writer_expire_at, t.generation
                INTO v_writer, v_writer_expire_at, v_generation
                FROM {table_name} t WHERE t.name = p_name;

            IF v_writer = p_token AND v_writer_expire_at > v_now THEN
                UPDATE {table_name} SET writer_expire_at = v_expire_at
                    WHERE name = p_name;
                RETURN QUERY SELECT v_generation, FALSE;
                RETURN;
            END IF;

            IF v_writer IS NOT NULL AND v_writer_expire_at > v_now THEN
                IF p_intent THEN
                    INSERT INTO {table_name}_holders
                        (name, token, kind, expire_at)
                        VALUES (p_name, p_token, 'i', v_expire_at)
                        ON CONFLICT (name, token, kind)
                        DO UPDATE SET expire_at = EXCLUDED.expire_at;
                END IF;
                RETURN;
            END IF;

            SELECT count(*) INTO v_readers FROM {table_name}_holders h
                WHERE h.name = p_name AND h.kind = 'r';
            IF v_readers > 0 THEN
                IF p_intent THEN
                    INSERT INTO {table_name}_holders
                        (name, token, kind, expire_at)
                        VALUES (p_name, p_token, 'i', v_expire_at)
                        ON CONFLICT (name, token, kind)
                        DO UPDATE SET expire_at = EXCLUDED.expire_at;
                END IF;
                RETURN;
            END IF;

            DELETE FROM {table_name}_holders
                WHERE name = p_name AND token = p_token AND kind = 'i';
            UPDATE {table_name}
                SET generation = generation + 1,
                    writer = p_token,
                    writer_expire_at = v_expire_at
                WHERE name = p_name
                RETURNING generation INTO v_generation;
            RETURN QUERY SELECT v_generation, (v_writer IS NOT NULL);
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_DOWNGRADE = """
        CREATE OR REPLACE FUNCTION {table_name}_downgrade(
            p_name TEXT, p_token TEXT, p_duration DOUBLE PRECISION
        ) RETURNS BIGINT AS $$
        DECLARE
            v_now TIMESTAMPTZ := NOW();
            v_generation BIGINT;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            SELECT t.generation INTO v_generation FROM {table_name} t
                WHERE t.name = p_name AND t.writer = p_token
                  AND t.writer_expire_at > v_now;
            IF v_generation IS NULL THEN
                RETURN NULL;
            END IF;
            UPDATE {table_name}
                SET writer = NULL, writer_expire_at = NULL
                WHERE name = p_name;
            INSERT INTO {table_name}_holders (name, token, kind, expire_at)
                VALUES (
                    p_name, p_token, 'r',
                    v_now + make_interval(secs => p_duration)
                )
                ON CONFLICT (name, token, kind)
                DO UPDATE SET expire_at = EXCLUDED.expire_at;
            RETURN v_generation;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_ACQUIRE_READ = "SELECT {table_name}_acquire_read($1, $2, $3);"
    _SQL_ACQUIRE_WRITE = (
        "SELECT * FROM {table_name}_acquire_write($1, $2, $3, $4);"
    )
    _SQL_DOWNGRADE = "SELECT {table_name}_downgrade($1, $2, $3);"

    _SQL_RELEASE_READ = """
        DELETE FROM {table_name}_holders
        WHERE name = $1 AND token = $2 AND kind = 'r' AND expire_at >= NOW()
        RETURNING 1;
    """

    _SQL_RELEASE_WRITE = """
        UPDATE {table_name}
        SET writer = NULL, writer_expire_at = NULL
        WHERE name = $1 AND writer = $2 AND writer_expire_at >= NOW()
        RETURNING 1;
    """

    _SQL_CANCEL_INTENT = """
        DELETE FROM {table_name}_holders
        WHERE name = $1 AND token = $2 AND kind = 'i' AND expire_at >= NOW()
        RETURNING 1;
    """

    _SQL_STATE = """
        SELECT t.generation,
               (t.writer IS NOT NULL AND t.writer_expire_at >= NOW())
                   AS writing,
               (SELECT count(*) FROM {table_name}_holders h
                    WHERE h.name = t.name AND h.kind = 'r'
                      AND h.expire_at >= NOW()) AS readers,
               (SELECT count(*) FROM {table_name}_holders h
                    WHERE h.name = t.name AND h.kind = 'i'
                      AND h.expire_at >= NOW()) AS waiting_writers
        FROM {table_name} t WHERE t.name = $1;
    """

    _SQL_OWNED_READ = """
        SELECT 1 FROM {table_name}_holders
        WHERE name = $1 AND token = $2 AND kind = 'r' AND expire_at >= NOW();
    """

    _SQL_OWNED_WRITE = """
        SELECT 1 FROM {table_name}
        WHERE name = $1 AND writer = $2 AND writer_expire_at >= NOW();
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
            str,
            Doc(
                """
                Table that stores read-write locks. A side table named
                `{table_name}_holders` stores reader leases and writer
                intents. Auto-created on first connect (set
                `auto_migrate=False` to opt out).
                """
            ),
        ] = "grelmicro_rwlocks",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the adapter creates the tables and
                SQL functions on `__aenter__`. Set to False when the schema
                is managed by your own migration tool.
                """
            ),
        ] = True,
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
        self._auto_migrate = auto_migrate
        self._sql = {
            key: template.format(table_name=table_name)
            for key, template in {
                "acquire_read": self._SQL_ACQUIRE_READ,
                "acquire_write": self._SQL_ACQUIRE_WRITE,
                "downgrade": self._SQL_DOWNGRADE,
                "release_read": self._SQL_RELEASE_READ,
                "release_write": self._SQL_RELEASE_WRITE,
                "cancel_intent": self._SQL_CANCEL_INTENT,
                "state": self._SQL_STATE,
                "owned_read": self._SQL_OWNED_READ,
                "owned_write": self._SQL_OWNED_WRITE,
            }.items()
        }
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
        """Open the adapter and install the schema when `auto_migrate=True`."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        if self._auto_migrate:  # pragma: no branch
            await self._migrate()
        return self

    async def _migrate(self) -> None:
        """Install the schema, guarded so replicas do not race."""
        async with (
            self._provider.client.acquire() as conn,
            conn.transaction(),
        ):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table_name
            )
            for sql in (
                self._SQL_CREATE_TABLES,
                self._SQL_CREATE_FN_ACQUIRE_READ,
                self._SQL_CREATE_FN_ACQUIRE_WRITE,
                self._SQL_CREATE_FN_DOWNGRADE,
            ):
                await conn.execute(
                    sql.format(
                        table_name=self._table_name,
                        lock_namespace=_READ_WRITE_ADVISORY_NAMESPACE,
                    )
                )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def acquire_read(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a read lease, returning the generation or `None`."""
        generation = await self._provider.client.fetchval(
            self._sql["acquire_read"], name, token, duration
        )
        return int(generation) if generation is not None else None

    async def acquire_write(
        self, *, name: str, token: str, duration: float, intent: bool = True
    ) -> WriteGrant | None:
        """Acquire the write lease, returning the grant or `None`."""
        row = await self._provider.client.fetchrow(
            self._sql["acquire_write"], name, token, duration, intent
        )
        if row is None:
            return None
        return WriteGrant(
            fencing_token=int(row["r_fence"]),
            poisoned=bool(row["r_poisoned"]),
        )

    async def release_read(self, *, name: str, token: str) -> bool:
        """Drop a read lease."""
        return bool(
            await self._provider.client.fetchval(
                self._sql["release_read"], name, token
            )
        )

    async def release_write(self, *, name: str, token: str) -> bool:
        """Drop the write lease, leaving the lock clean."""
        return bool(
            await self._provider.client.fetchval(
                self._sql["release_write"], name, token
            )
        )

    async def cancel_intent(self, *, name: str, token: str) -> bool:
        """Withdraw a writer intent."""
        return bool(
            await self._provider.client.fetchval(
                self._sql["cancel_intent"], name, token
            )
        )

    async def downgrade(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Turn a held write lease into a read lease."""
        generation = await self._provider.client.fetchval(
            self._sql["downgrade"], name, token, duration
        )
        return int(generation) if generation is not None else None

    async def state(self, *, name: str) -> ReadWriteLockState:
        """Return a point-in-time view of the lock."""
        row = await self._provider.client.fetchrow(self._sql["state"], name)
        if row is None:
            return ReadWriteLockState(
                generation=0, writing=False, readers=0, waiting_writers=0
            )
        return ReadWriteLockState(
            generation=int(row["generation"]),
            writing=bool(row["writing"]),
            readers=int(row["readers"]),
            waiting_writers=int(row["waiting_writers"]),
        )

    async def owned_read(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds a live read lease."""
        return bool(
            await self._provider.client.fetchval(
                self._sql["owned_read"], name, token
            )
        )

    async def owned_write(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds the live write lease."""
        return bool(
            await self._provider.client.fetchval(
                self._sql["owned_write"], name, token
            )
        )


class PostgresScheduleAdapter(ScheduleBackend):
    """Postgres Schedule Adapter.

    Wraps a `PostgresProvider` and implements the `ScheduleBackend` protocol
    for durable distributed cron. The `last_fired` epoch is stored as a
    `DOUBLE PRECISION` column on a row keyed by `name`, and the claim decision
    runs in a single `INSERT ... ON CONFLICT` statement, so the compare-and-set
    is atomic across processes and machines.

    Pass an explicit `provider=` to share a pool with other components, or rely
    on the default `env_prefix=` to build one from environment variables.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
                CREATE TABLE IF NOT EXISTS {table_name} (
                    name TEXT PRIMARY KEY,
                    last_fired DOUBLE PRECISION NOT NULL
                );
                """

    _SQL_CLAIM = """
                INSERT INTO {table_name} (name, last_fired)
                VALUES ($1, $2)
                ON CONFLICT (name) DO UPDATE
                SET last_fired = EXCLUDED.last_fired
                WHERE {table_name}.last_fired < EXCLUDED.last_fired
                RETURNING 1;
                """

    _SQL_LAST_FIRED = """
        SELECT last_fired FROM {table_name} WHERE name = $1;
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
            str, Doc("The table name to store the schedules.")
        ] = "schedules",
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
        self._claim_sql = self._SQL_CLAIM.format(table_name=table_name)
        self._last_fired_sql = self._SQL_LAST_FIRED.format(
            table_name=table_name
        )
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
        """Open the adapter and its provider when owned."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        await self._migrate()
        return self

    async def _migrate(self) -> None:
        """Install the schema, guarded so replicas do not race.

        `CREATE TABLE IF NOT EXISTS` checks and creates in two steps, so
        two workers starting together can both pass the check and one then
        fails on the row type the table creates.
        """
        async with (
            self._provider.client.acquire() as conn,
            conn.transaction(),
        ):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table_name
            )
            await conn.execute(
                self._SQL_CREATE_TABLE_IF_NOT_EXISTS.format(
                    table_name=self._table_name
                ),
            )

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
        """Atomically claim the fire at `due`."""
        return bool(
            await self._provider.client.fetchval(self._claim_sql, name, due)
        )

    async def last_fired(self, name: str) -> float | None:
        """Return the stored `last_fired` epoch, or `None`."""
        stored = await self._provider.client.fetchval(
            self._last_fired_sql, name
        )
        return float(stored) if stored is not None else None


_LEADER_ELECTION_ADVISORY_NAMESPACE = 0x67726C65_2D656C65
"""Advisory-lock namespace for leader election.

`hashtextextended` is the Postgres 64-bit text hash with a configurable
seed. A distinct seed gives election names their own 64-bit lock id space,
isolated from any other advisory lock in the same database.
"""


class PostgresLeaderElectionAdapter:
    """Postgres leader election adapter.

    Wraps a `PostgresProvider` and implements the `LeaderElectionBackend`
    protocol on top of a single `{table_name}` row per election. Every
    `acquire_or_renew` runs a PL/pgSQL function that holds
    `pg_advisory_xact_lock` for the election name, so the read, the
    acquire/renew decision, and the write apply atomically across replicas.

    The expired row is kept in place so a takeover can read the previous
    holder and bump `transitions`. Only `acquire_or_renew`, `release`, and
    `get` treat an expired lease as vacant.

    Pass an explicit `provider=` to share a pool with other components, or
    rely on the default `env_prefix=` to build one from environment
    variables.

    Example:
    ```python
    from grelmicro.coordination.postgres import PostgresLeaderElectionAdapter
    from grelmicro.providers.postgres import PostgresProvider

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    backend = PostgresLeaderElectionAdapter(provider=postgres)
    ```
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    is_shared: ClassVar[bool] = True

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            name TEXT PRIMARY KEY,
            holder TEXT NOT NULL,
            lease_duration DOUBLE PRECISION NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL,
            renewed_at TIMESTAMPTZ NOT NULL,
            transitions INT NOT NULL DEFAULT 0,
            metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
    """

    _SQL_CREATE_FN_ACQUIRE_OR_RENEW = """
        CREATE OR REPLACE FUNCTION {table_name}_le_acquire_or_renew(
            p_name TEXT,
            p_token TEXT,
            p_duration DOUBLE PRECISION,
            p_metadata JSONB
        ) RETURNS TABLE(
            r_holder TEXT,
            r_lease_duration DOUBLE PRECISION,
            r_acquired_at TIMESTAMPTZ,
            r_renewed_at TIMESTAMPTZ,
            r_transitions INT,
            r_metadata JSONB
        ) AS $$
        DECLARE
            v_now TIMESTAMPTZ := NOW();
            v_holder TEXT;
            v_lease_duration DOUBLE PRECISION;
            v_acquired_at TIMESTAMPTZ;
            v_renewed_at TIMESTAMPTZ;
            v_transitions INT;
            v_expired BOOLEAN;
            v_new_transitions INT;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            SELECT t.holder, t.lease_duration, t.acquired_at,
                   t.renewed_at, t.transitions
                INTO v_holder, v_lease_duration, v_acquired_at,
                     v_renewed_at, v_transitions
                FROM {table_name} t WHERE t.name = p_name;

            v_expired := v_holder IS NULL OR v_now >= (
                v_renewed_at + make_interval(secs => v_lease_duration)
            );

            IF NOT v_expired AND v_holder <> p_token THEN
                RETURN QUERY SELECT
                    v_holder, v_lease_duration, v_acquired_at,
                    v_renewed_at, v_transitions,
                    (SELECT t.metadata FROM {table_name} t
                        WHERE t.name = p_name);
                RETURN;
            END IF;

            IF NOT v_expired THEN
                UPDATE {table_name}
                    SET renewed_at = v_now,
                        lease_duration = p_duration,
                        metadata = p_metadata
                    WHERE name = p_name;
                RETURN QUERY SELECT
                    p_token, p_duration, v_acquired_at, v_now,
                    v_transitions, p_metadata;
                RETURN;
            END IF;

            IF v_holder IS NULL OR v_holder = p_token THEN
                v_new_transitions := COALESCE(v_transitions, 0);
            ELSE
                v_new_transitions := v_transitions + 1;
            END IF;

            INSERT INTO {table_name} (
                name, holder, lease_duration, acquired_at,
                renewed_at, transitions, metadata
            )
            VALUES (
                p_name, p_token, p_duration, v_now,
                v_now, v_new_transitions, p_metadata
            )
            ON CONFLICT (name) DO UPDATE
                SET holder = EXCLUDED.holder,
                    lease_duration = EXCLUDED.lease_duration,
                    acquired_at = EXCLUDED.acquired_at,
                    renewed_at = EXCLUDED.renewed_at,
                    transitions = EXCLUDED.transitions,
                    metadata = EXCLUDED.metadata;
            RETURN QUERY SELECT
                p_token, p_duration, v_now, v_now,
                v_new_transitions, p_metadata;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_RELEASE = """
        DELETE FROM {table_name}
        WHERE name = $1 AND holder = $2
            AND NOW() < renewed_at + make_interval(secs => lease_duration)
        RETURNING 1;
    """

    _SQL_GET = """
        SELECT holder, lease_duration, acquired_at, renewed_at,
               transitions, metadata
        FROM {table_name}
        WHERE name = $1
            AND NOW() < renewed_at + make_interval(secs => lease_duration);
    """

    _SQL_ACQUIRE_OR_RENEW = (
        "SELECT * FROM {table_name}_le_acquire_or_renew($1, $2, $3, $4::jsonb);"
    )

    def __init__(
        self,
        *,
        provider: Annotated[
            PostgresProvider | None,
            Doc(
                """
                A pre-built `PostgresProvider`. When set, the backend
                borrows the provider's pool and does not manage its
                lifecycle.
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `PostgresProvider` when `provider` is not set. Defaults
                to `POSTGRES_`. Use a custom prefix to split pools.
                """
            ),
        ] = "POSTGRES_",
        table_name: Annotated[
            str,
            Doc(
                """
                Table that stores leader election leases. Auto-created on
                first connect (set `auto_migrate=False` to opt out).
                """
            ),
        ] = "grelmicro_leader_election",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the backend creates the table and
                SQL function on `__aenter__`. Set to False when the schema
                is managed by your own migration tool.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the leader election backend."""
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
        self._auto_migrate = auto_migrate
        self._acquire_or_renew_sql = self._SQL_ACQUIRE_OR_RENEW.format(
            table_name=table_name
        )
        self._release_sql = self._SQL_RELEASE.format(table_name=table_name)
        self._get_sql = self._SQL_GET.format(table_name=table_name)

    @property
    def provider(self) -> PostgresProvider:
        """The bound `PostgresProvider`."""
        return self._provider

    def _rebind_provider(self, provider: PostgresProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the backend and install the schema when `auto_migrate=True`."""
        if self._owns_provider:
            await self._provider.__aenter__()
        if self._auto_migrate:  # pragma: no branch
            await self._migrate()
        return self

    async def _migrate(self) -> None:
        """Install the schema, guarded so replicas do not race.

        `CREATE TABLE IF NOT EXISTS` checks and creates in two steps, so
        two workers starting together can both pass the check and one then
        fails on the row type the table creates.
        """
        async with (
            self._provider.client.acquire() as conn,
            conn.transaction(),
        ):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table_name
            )
            for sql in (
                self._SQL_CREATE_TABLE,
                self._SQL_CREATE_FN_ACQUIRE_OR_RENEW,
            ):
                await conn.execute(
                    sql.format(
                        table_name=self._table_name,
                        lock_namespace=_LEADER_ELECTION_ADVISORY_NAMESPACE,
                    )
                )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def acquire_or_renew(
        self,
        *,
        name: str,
        token: str,
        duration: float,
        metadata: Mapping[str, str] | None = None,
    ) -> LeaderRecord:
        """Acquire or renew the lease, returning the resulting record."""
        payload = json.dumps(dict(metadata or {}))
        row = await self._provider.client.fetchrow(
            self._acquire_or_renew_sql, name, token, duration, payload
        )
        return self._unpack(row)

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lease when held by `token`."""
        return bool(
            await self._provider.client.fetchval(self._release_sql, name, token)
        )

    async def get(self, *, name: str) -> LeaderRecord | None:
        """Return the current live record, or `None`."""
        row = await self._provider.client.fetchrow(self._get_sql, name)
        if row is None:
            return None
        return LeaderRecord(
            holder=row["holder"],
            lease_duration=float(row["lease_duration"]),
            acquired_at=row["acquired_at"],
            renewed_at=row["renewed_at"],
            transitions=int(row["transitions"]),
            metadata=_decode_metadata(row["metadata"]),
        )

    @staticmethod
    def _unpack(row: Any) -> LeaderRecord:  # noqa: ANN401
        """Build a `LeaderRecord` from a function result row."""
        return LeaderRecord(
            holder=row["r_holder"],
            lease_duration=float(row["r_lease_duration"]),
            acquired_at=row["r_acquired_at"],
            renewed_at=row["r_renewed_at"],
            transitions=int(row["r_transitions"]),
            metadata=_decode_metadata(row["r_metadata"]),
        )


def _decode_metadata(value: Any) -> dict[str, str]:  # noqa: ANN401
    """Decode a jsonb column value into a plain string mapping.

    asyncpg returns jsonb as a JSON string unless a codec is registered, so
    decode it back to a dict. A dict is passed through unchanged.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        return dict(json.loads(value))
    return dict(value)
