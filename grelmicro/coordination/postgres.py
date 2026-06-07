"""Postgres coordination adapters."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.coordination.abc import LeaderRecord, LockBackend
from grelmicro.providers.postgres import PostgresProvider

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


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


_LEADER_ELECTION_ADVISORY_NAMESPACE = 0x67726C65_2D656C65
"""Advisory-lock namespace for leader election.

`hashtextextended` is the Postgres 64-bit text hash with a configurable
seed. A distinct seed gives election names their own 64-bit lock id space,
isolated from any other advisory lock in the same database.
"""


class PostgresLeaderElectionBackend:
    """Postgres leader election backend.

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
    from grelmicro.coordination.postgres import PostgresLeaderElectionBackend
    from grelmicro.providers.postgres import PostgresProvider

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    backend = PostgresLeaderElectionBackend(provider=postgres)
    ```
    """

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
            pool = self._provider.client
            for sql in (
                self._SQL_CREATE_TABLE,
                self._SQL_CREATE_FN_ACQUIRE_OR_RENEW,
            ):
                await pool.execute(
                    sql.format(
                        table_name=self._table_name,
                        lock_namespace=_LEADER_ELECTION_ADVISORY_NAMESPACE,
                    )
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
