"""Postgres rate-limiter adapter."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Self, assert_never

from typing_extensions import Doc

from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.ratelimiter.sliding_window import SlidingWindowConfig
from grelmicro.resilience.ratelimiter.token_bucket import TokenBucketConfig

if TYPE_CHECKING:
    from types import TracebackType

    from asyncpg import Pool


class PostgresRateLimiterAdapter(RateLimiterBackend):
    """Postgres rate limiter adapter.

    Wraps a `PostgresProvider` and supports both
    [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
    and [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig]
    algorithm configs via PL/pgSQL functions. Concurrent writes for
    the same key are serialized with `pg_advisory_xact_lock`. Safe
    across processes and machines.

    Example:
    ```python
    from grelmicro.providers.postgres import PostgresProvider
    from grelmicro.resilience import RateLimiter
    from grelmicro.resilience.ratelimiter.postgres import (
        PostgresRateLimiterAdapter,
    )


    async def main() -> None:
        provider = PostgresProvider("postgresql://localhost:5432/app")
        async with provider, PostgresRateLimiterAdapter(provider=provider):
            rl = RateLimiter.token_bucket("api", capacity=10, refill_rate=1)
            await rl.acquire(key="u1")
    ```

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            key TEXT PRIMARY KEY,
            tokens DOUBLE PRECISION NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS {table_name}_updated_at_idx
            ON {table_name} (updated_at);
    """

    _SQL_CREATE_FN_TB_ACQUIRE = """
        CREATE OR REPLACE FUNCTION {table_name}_tb_acquire(
            p_key TEXT,
            p_capacity DOUBLE PRECISION,
            p_refill_rate DOUBLE PRECISION,
            p_cost DOUBLE PRECISION
        ) RETURNS TABLE(
            allowed BOOLEAN,
            remaining INT,
            retry_after DOUBLE PRECISION,
            reset_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_now TIMESTAMPTZ := clock_timestamp();
            v_tokens DOUBLE PRECISION;
            v_last TIMESTAMPTZ;
            v_new DOUBLE PRECISION;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtext(p_key));
            SELECT tokens, updated_at INTO v_tokens, v_last
                FROM {table_name} WHERE key = p_key;
            IF v_tokens IS NULL THEN
                v_tokens := p_capacity;
                v_last := v_now;
            END IF;
            v_new := LEAST(
                p_capacity,
                v_tokens + EXTRACT(EPOCH FROM (v_now - v_last)) * p_refill_rate
            );
            IF v_new >= p_cost THEN
                INSERT INTO {table_name} (key, tokens, updated_at)
                VALUES (p_key, v_new - p_cost, v_now)
                ON CONFLICT (key) DO UPDATE
                    SET tokens = EXCLUDED.tokens, updated_at = EXCLUDED.updated_at;
                RETURN QUERY SELECT
                    TRUE,
                    FLOOR(v_new - p_cost)::INT,
                    0::double precision,
                    (p_capacity - (v_new - p_cost)) / p_refill_rate;
            ELSE
                INSERT INTO {table_name} (key, tokens, updated_at)
                VALUES (p_key, v_new, v_now)
                ON CONFLICT (key) DO UPDATE
                    SET tokens = EXCLUDED.tokens, updated_at = EXCLUDED.updated_at;
                RETURN QUERY SELECT
                    FALSE,
                    FLOOR(v_new)::INT,
                    (p_cost - v_new) / p_refill_rate,
                    (p_capacity - v_new) / p_refill_rate;
            END IF;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_TB_PEEK = """
        CREATE OR REPLACE FUNCTION {table_name}_tb_peek(
            p_key TEXT,
            p_capacity DOUBLE PRECISION,
            p_refill_rate DOUBLE PRECISION
        ) RETURNS TABLE(
            allowed BOOLEAN,
            remaining INT,
            retry_after DOUBLE PRECISION,
            reset_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_now TIMESTAMPTZ := clock_timestamp();
            v_tokens DOUBLE PRECISION;
            v_last TIMESTAMPTZ;
            v_new DOUBLE PRECISION;
        BEGIN
            SELECT tokens, updated_at INTO v_tokens, v_last
                FROM {table_name} WHERE key = p_key;
            IF v_tokens IS NULL THEN
                v_tokens := p_capacity;
                v_last := v_now;
            END IF;
            v_new := LEAST(
                p_capacity,
                v_tokens + EXTRACT(EPOCH FROM (v_now - v_last)) * p_refill_rate
            );
            IF v_new >= 1 THEN
                RETURN QUERY SELECT
                    TRUE,
                    FLOOR(v_new)::INT,
                    0::double precision,
                    (p_capacity - v_new) / p_refill_rate;
            ELSE
                RETURN QUERY SELECT
                    FALSE,
                    FLOOR(v_new)::INT,
                    (1 - v_new) / p_refill_rate,
                    (p_capacity - v_new) / p_refill_rate;
            END IF;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_GCRA_ACQUIRE = """
        CREATE OR REPLACE FUNCTION {table_name}_gcra_acquire(
            p_key TEXT,
            p_limit DOUBLE PRECISION,
            p_window DOUBLE PRECISION,
            p_cost DOUBLE PRECISION
        ) RETURNS TABLE(
            allowed BOOLEAN,
            remaining INT,
            retry_after DOUBLE PRECISION,
            reset_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_now DOUBLE PRECISION := EXTRACT(EPOCH FROM clock_timestamp());
            v_emission DOUBLE PRECISION := p_window / p_limit;
            v_increment DOUBLE PRECISION;
            v_tat DOUBLE PRECISION;
            v_new_tat DOUBLE PRECISION;
            v_diff DOUBLE PRECISION;
            v_remaining INT;
        BEGIN
            v_increment := v_emission * p_cost;
            PERFORM pg_advisory_xact_lock(hashtext(p_key));
            SELECT tokens INTO v_tat FROM {table_name} WHERE key = p_key;
            IF v_tat IS NULL THEN
                v_tat := v_now;
            END IF;
            v_new_tat := GREATEST(v_tat, v_now) + v_increment;
            v_diff := v_now - (v_new_tat - v_emission * p_limit);
            v_remaining := FLOOR(v_diff / v_emission + 0.5)::INT;
            IF v_remaining < 0 THEN
                RETURN QUERY SELECT
                    FALSE,
                    0,
                    GREATEST(0::double precision, -v_diff),
                    GREATEST(0::double precision, v_tat - v_now);
            ELSE
                INSERT INTO {table_name} (key, tokens, updated_at)
                VALUES (p_key, v_new_tat, clock_timestamp())
                ON CONFLICT (key) DO UPDATE
                    SET tokens = EXCLUDED.tokens, updated_at = EXCLUDED.updated_at;
                RETURN QUERY SELECT
                    TRUE,
                    v_remaining,
                    0::double precision,
                    v_new_tat - v_now;
            END IF;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_GCRA_PEEK = """
        CREATE OR REPLACE FUNCTION {table_name}_gcra_peek(
            p_key TEXT,
            p_limit DOUBLE PRECISION,
            p_window DOUBLE PRECISION
        ) RETURNS TABLE(
            allowed BOOLEAN,
            remaining INT,
            retry_after DOUBLE PRECISION,
            reset_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_now DOUBLE PRECISION := EXTRACT(EPOCH FROM clock_timestamp());
            v_emission DOUBLE PRECISION := p_window / p_limit;
            v_tat DOUBLE PRECISION;
            v_new_tat DOUBLE PRECISION;
            v_diff DOUBLE PRECISION;
            v_remaining INT;
            v_retry DOUBLE PRECISION;
        BEGIN
            SELECT tokens INTO v_tat FROM {table_name} WHERE key = p_key;
            IF v_tat IS NULL THEN
                v_tat := v_now;
            END IF;
            v_new_tat := GREATEST(v_tat, v_now);
            v_diff := v_now - (v_new_tat - p_window);
            v_remaining := FLOOR(v_diff / v_emission + 0.5)::INT;
            IF v_remaining <= 0 THEN
                IF v_remaining < 0 THEN
                    v_retry := -v_diff;
                ELSE
                    v_retry := v_emission - v_diff;
                END IF;
                RETURN QUERY SELECT
                    FALSE,
                    0,
                    GREATEST(0::double precision, v_retry),
                    GREATEST(0::double precision, v_tat - v_now);
            ELSE
                RETURN QUERY SELECT
                    TRUE,
                    v_remaining,
                    0::double precision,
                    GREATEST(0::double precision, v_new_tat - v_now);
            END IF;
        END;
        $$ LANGUAGE plpgsql;
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
        prefix: Annotated[
            str,
            Doc(
                """
                Prefix prepended to every key the adapter writes. Use
                it to avoid collisions with other consumers of the
                same Postgres table.
                """,
            ),
        ] = "",
        table_name: Annotated[
            str,
            Doc(
                """
                Table that stores rate-limit state. Auto-created on
                first connect (set `auto_migrate=False` to opt out).
                """,
            ),
        ] = "grelmicro_rate_limiter",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the adapter creates the table
                and SQL functions on `__aenter__`. Set to False when
                the schema is managed by your own migration tool.
                """,
            ),
        ] = True,
    ) -> None:
        """Initialize the rate limiter adapter."""
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
        self._prefix = prefix
        self._table_name = table_name
        self._auto_migrate = auto_migrate

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
        if self._auto_migrate:
            pool = self._provider.client
            for sql in (
                self._SQL_CREATE_TABLE,
                self._SQL_CREATE_FN_TB_ACQUIRE,
                self._SQL_CREATE_FN_TB_PEEK,
                self._SQL_CREATE_FN_GCRA_ACQUIRE,
                self._SQL_CREATE_FN_GCRA_PEEK,
            ):
                await pool.execute(sql.format(table_name=self._table_name))
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

    def bind(
        self,
        config: TokenBucketConfig | SlidingWindowConfig,
    ) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config.

        Each strategy targets a pair of PL/pgSQL functions installed
        on `__aenter__`. Functions read and write a single row of
        `{table_name}` per key.
        """
        pool = self._provider.client
        match config:
            case TokenBucketConfig():
                return _PostgresTokenBucket(
                    pool, self._prefix, self._table_name, config
                )
            case SlidingWindowConfig():
                return _PostgresGCRA(
                    pool, self._prefix, self._table_name, config
                )
        assert_never(config)


class _PostgresGCRA(RateLimiterStrategy):
    """Postgres GCRA strategy. Private.

    Prepends a per-algorithm discriminator to every key so a GCRA
    limiter and a token-bucket limiter sharing the same name cannot
    collide on the shared `{table_name}` table.
    """

    _ALGO_PREFIX = "gcra:"

    _SQL_ACQUIRE = "SELECT * FROM {table_name}_gcra_acquire($1, $2, $3, $4);"
    _SQL_PEEK = "SELECT * FROM {table_name}_gcra_peek($1, $2, $3);"
    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = $1;"

    def __init__(
        self,
        pool: Pool,
        prefix: str,
        table_name: str,
        config: SlidingWindowConfig,
    ) -> None:
        self._pool = pool
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._acquire_sql = self._SQL_ACQUIRE.format(table_name=table_name)
        self._peek_sql = self._SQL_PEEK.format(table_name=table_name)
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
        self._limit = config.limit
        self._window = config.window

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (GCRA)."""
        row = await self._pool.fetchrow(
            self._acquire_sql,
            f"{self._key_prefix}{key}",
            float(self._limit),
            float(self._window),
            float(cost),
        )
        return RateLimitResult(
            allowed=bool(row["allowed"]),
            limit=self._limit,
            remaining=int(row["remaining"]),
            retry_after=float(row["retry_after"]),
            reset_after=float(row["reset_after"]),
        )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (GCRA)."""
        row = await self._pool.fetchrow(
            self._peek_sql,
            f"{self._key_prefix}{key}",
            float(self._limit),
            float(self._window),
        )
        return RateLimitResult(
            allowed=bool(row["allowed"]),
            limit=self._limit,
            remaining=int(row["remaining"]),
            retry_after=float(row["retry_after"]),
            reset_after=float(row["reset_after"]),
        )

    async def reset(self, *, key: str) -> None:
        """Async reset (GCRA)."""
        await self._pool.execute(self._delete_sql, f"{self._key_prefix}{key}")


class _PostgresTokenBucket(RateLimiterStrategy):
    """Postgres token-bucket strategy. Private.

    Continuous refill by `refill_rate` (tokens/sec). `acquire` runs
    a PL/pgSQL function that holds `pg_advisory_xact_lock(hashtext(key))`
    while it reads, refills, decrements, and writes the new state.
    `peek` reads the same state without locking. Prepends a
    per-algorithm discriminator to every key so a token-bucket
    limiter and a GCRA limiter sharing the same name cannot collide
    on the shared `{table_name}` table.
    """

    _ALGO_PREFIX = "tb:"

    _SQL_ACQUIRE = "SELECT * FROM {table_name}_tb_acquire($1, $2, $3, $4);"
    _SQL_PEEK = "SELECT * FROM {table_name}_tb_peek($1, $2, $3);"
    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = $1;"

    def __init__(
        self,
        pool: Pool,
        prefix: str,
        table_name: str,
        config: TokenBucketConfig,
    ) -> None:
        self._pool = pool
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._acquire_sql = self._SQL_ACQUIRE.format(table_name=table_name)
        self._peek_sql = self._SQL_PEEK.format(table_name=table_name)
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
        self._capacity = config.capacity
        self._refill_rate = config.refill_rate

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (token bucket)."""
        row = await self._pool.fetchrow(
            self._acquire_sql,
            f"{self._key_prefix}{key}",
            float(self._capacity),
            float(self._refill_rate),
            float(cost),
        )
        return RateLimitResult(
            allowed=bool(row["allowed"]),
            limit=self._capacity,
            remaining=int(row["remaining"]),
            retry_after=float(row["retry_after"]),
            reset_after=float(row["reset_after"]),
        )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (token bucket)."""
        row = await self._pool.fetchrow(
            self._peek_sql,
            f"{self._key_prefix}{key}",
            float(self._capacity),
            float(self._refill_rate),
        )
        return RateLimitResult(
            allowed=bool(row["allowed"]),
            limit=self._capacity,
            remaining=int(row["remaining"]),
            retry_after=float(row["retry_after"]),
            reset_after=float(row["reset_after"]),
        )

    async def reset(self, *, key: str) -> None:
        """Async reset (token bucket)."""
        await self._pool.execute(self._delete_sql, f"{self._key_prefix}{key}")
