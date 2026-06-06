"""SQLite rate-limiter adapter."""

from __future__ import annotations

import asyncio
import math
import re
from time import time
from typing import TYPE_CHECKING, Annotated, Self, assert_never

from typing_extensions import Doc

from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience._protocol import (
    RateLimiterBackend,
    RateLimiterStrategy,
    RateLimitResult,
)
from grelmicro.resilience.ratelimiter.sliding_window import SlidingWindowConfig
from grelmicro.resilience.ratelimiter.token_bucket import TokenBucketConfig

if TYPE_CHECKING:
    from types import TracebackType

    import aiosqlite


class SQLiteRateLimiterAdapter(RateLimiterBackend):
    """SQLite rate limiter adapter.

    Internal machinery. Most code should reach SQLite rate limiting
    through a `SQLiteProvider` and `RateLimiters(provider)`, not by
    constructing this adapter directly. The adapter exists for expert
    wiring and for the provider to build.

    Borrows the connection and a shared lock from a `SQLiteProvider`
    and supports both
    [`TokenBucketConfig`][grelmicro.resilience.TokenBucketConfig]
    and [`SlidingWindowConfig`][grelmicro.resilience.SlidingWindowConfig]
    algorithm configs. Each acquire runs a read-modify-write inside a
    `BEGIN IMMEDIATE` transaction. The provider's lock serializes the
    single connection within the process, and the transaction's write
    lock serializes across processes sharing the same file. State
    survives process restarts. For multi-replica coordination, use
    [`RedisRateLimiterAdapter`][grelmicro.resilience.RedisRateLimiterAdapter]
    or
    [`PostgresRateLimiterAdapter`][grelmicro.resilience.PostgresRateLimiterAdapter].

    Read more in the [Rate Limiter](../resilience/rate-limiter.md) docs.
    """

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            key TEXT PRIMARY KEY,
            tokens REAL NOT NULL,
            updated_at REAL NOT NULL
        );
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
                """
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `SQLiteProvider` when `provider` is not set. Resolves
                the path from `SQLITE_PATH` by default.
                """
            ),
        ] = "SQLITE_",
        prefix: Annotated[
            str,
            Doc(
                """
                Prefix prepended to every key the adapter writes. Use
                it to avoid collisions with other consumers of the
                same SQLite table.
                """
            ),
        ] = "",
        table_name: Annotated[
            str,
            Doc(
                """
                Table that stores rate-limit state. Auto-created on
                first connect (set `auto_migrate=False` to opt out).
                """
            ),
        ] = "grelmicro_rate_limiter",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the adapter creates the table
                on `__aenter__`. Set to False when the schema is
                managed by your own migration tool.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the rate limiter adapter."""
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
        self._prefix = prefix
        self._table_name = table_name
        self._auto_migrate = auto_migrate
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
        """Open the adapter and install the schema when `auto_migrate=True`."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        if self._auto_migrate:  # pragma: no branch
            await self._provider.client.execute(
                self._SQL_CREATE_TABLE.format(table_name=self._table_name)
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        self._loop = None
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    def bind(
        self,
        config: TokenBucketConfig | SlidingWindowConfig,
    ) -> RateLimiterStrategy:
        """Build a strategy for the given algorithm config."""
        conn = self._provider.client
        lock = self._provider.connection_lock
        match config:
            case TokenBucketConfig():
                return _SQLiteTokenBucket(
                    conn, lock, self._prefix, self._table_name, config
                )
            case SlidingWindowConfig():
                return _SQLiteGCRA(
                    conn, lock, self._prefix, self._table_name, config
                )
        assert_never(config)


class _SQLiteTokenBucket(RateLimiterStrategy):
    """SQLite token-bucket strategy. Private.

    Continuous refill by `refill_rate` (tokens/sec). Prepends a
    per-algorithm discriminator to every key so a token-bucket limiter
    and a GCRA limiter sharing the same name cannot collide on the
    shared `{table_name}` table.
    """

    _ALGO_PREFIX = "tb:"

    _SQL_SELECT = "SELECT tokens, updated_at FROM {table_name} WHERE key = ?;"
    _SQL_UPSERT = """
        INSERT INTO {table_name} (key, tokens, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE
            SET tokens = excluded.tokens, updated_at = excluded.updated_at;
    """
    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = ?;"

    def __init__(
        self,
        conn: aiosqlite.Connection,
        lock: asyncio.Lock,
        prefix: str,
        table_name: str,
        config: TokenBucketConfig,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._select_sql = self._SQL_SELECT.format(table_name=table_name)
        self._upsert_sql = self._SQL_UPSERT.format(table_name=table_name)
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
        self._capacity = config.capacity
        self._refill_rate = config.refill_rate

    def _refill(self, tokens: float, last: float, now: float) -> float:
        return min(self._capacity, tokens + (now - last) * self._refill_rate)

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (token bucket)."""
        full_key = f"{self._key_prefix}{key}"
        now = time()
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE;")
            try:
                async with self._conn.execute(
                    self._select_sql, (full_key,)
                ) as cursor:
                    row = await cursor.fetchone()
                tokens, last = (self._capacity, now) if row is None else row
                tokens = self._refill(tokens, last, now)
                if tokens >= cost:
                    remaining = tokens - cost
                    await self._conn.execute(
                        self._upsert_sql, (full_key, remaining, now)
                    )
                    await self._conn.execute("COMMIT;")
                    return RateLimitResult(
                        allowed=True,
                        limit=int(self._capacity),
                        remaining=int(remaining),
                        retry_after=0.0,
                        reset_after=(self._capacity - remaining)
                        / self._refill_rate,
                    )
                await self._conn.execute(
                    self._upsert_sql, (full_key, tokens, now)
                )
                await self._conn.execute("COMMIT;")
            except BaseException:
                await self._conn.execute("ROLLBACK;")
                raise
            return RateLimitResult(
                allowed=False,
                limit=int(self._capacity),
                remaining=int(tokens),
                retry_after=(cost - tokens) / self._refill_rate,
                reset_after=(self._capacity - tokens) / self._refill_rate,
            )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (token bucket)."""
        full_key = f"{self._key_prefix}{key}"
        now = time()
        async with (
            self._lock,
            self._conn.execute(self._select_sql, (full_key,)) as cursor,
        ):
            row = await cursor.fetchone()
        tokens, last = (self._capacity, now) if row is None else row
        tokens = self._refill(tokens, last, now)
        if tokens >= 1.0:
            return RateLimitResult(
                allowed=True,
                limit=int(self._capacity),
                remaining=int(tokens),
                retry_after=0.0,
                reset_after=(self._capacity - tokens) / self._refill_rate,
            )
        return RateLimitResult(
            allowed=False,
            limit=int(self._capacity),
            remaining=int(tokens),
            retry_after=(1.0 - tokens) / self._refill_rate,
            reset_after=(self._capacity - tokens) / self._refill_rate,
        )

    async def reset(self, *, key: str) -> None:
        """Async reset (token bucket)."""
        full_key = f"{self._key_prefix}{key}"
        async with self._lock:
            await self._conn.execute(self._delete_sql, (full_key,))


class _SQLiteGCRA(RateLimiterStrategy):
    """SQLite GCRA strategy. Private.

    Stores the theoretical arrival time (TAT) in the `tokens` column.
    Prepends a per-algorithm discriminator to every key so a GCRA
    limiter and a token-bucket limiter sharing the same name cannot
    collide on the shared `{table_name}` table.
    """

    _ALGO_PREFIX = "gcra:"

    _SQL_SELECT = "SELECT tokens FROM {table_name} WHERE key = ?;"
    _SQL_UPSERT = """
        INSERT INTO {table_name} (key, tokens, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE
            SET tokens = excluded.tokens, updated_at = excluded.updated_at;
    """
    _SQL_DELETE = "DELETE FROM {table_name} WHERE key = ?;"

    def __init__(
        self,
        conn: aiosqlite.Connection,
        lock: asyncio.Lock,
        prefix: str,
        table_name: str,
        config: SlidingWindowConfig,
    ) -> None:
        self._conn = conn
        self._lock = lock
        self._key_prefix = f"{prefix}{self._ALGO_PREFIX}"
        self._select_sql = self._SQL_SELECT.format(table_name=table_name)
        self._upsert_sql = self._SQL_UPSERT.format(table_name=table_name)
        self._delete_sql = self._SQL_DELETE.format(table_name=table_name)
        self._limit = config.limit
        self._window = config.window
        self._emission_interval = config.window / config.limit

    async def acquire(self, *, key: str, cost: int) -> RateLimitResult:
        """Async acquire (GCRA)."""
        full_key = f"{self._key_prefix}{key}"
        now = time()
        increment = self._emission_interval * cost
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE;")
            try:
                async with self._conn.execute(
                    self._select_sql, (full_key,)
                ) as cursor:
                    row = await cursor.fetchone()
                tat = now if row is None else row[0]
                new_tat = max(tat, now) + increment
                allow_at = new_tat - self._window
                diff = now - allow_at
                remaining = math.floor(diff / self._emission_interval + 0.5)
                if remaining < 0:
                    await self._conn.execute("COMMIT;")
                    return RateLimitResult(
                        allowed=False,
                        limit=self._limit,
                        remaining=0,
                        retry_after=max(0.0, -diff),
                        reset_after=max(0.0, tat - now),
                    )
                await self._conn.execute(
                    self._upsert_sql, (full_key, new_tat, now)
                )
                await self._conn.execute("COMMIT;")
            except BaseException:
                await self._conn.execute("ROLLBACK;")
                raise
            return RateLimitResult(
                allowed=True,
                limit=self._limit,
                remaining=remaining,
                retry_after=0.0,
                reset_after=new_tat - now,
            )

    async def peek(self, *, key: str) -> RateLimitResult:
        """Async peek (GCRA)."""
        full_key = f"{self._key_prefix}{key}"
        now = time()
        async with (
            self._lock,
            self._conn.execute(self._select_sql, (full_key,)) as cursor,
        ):
            row = await cursor.fetchone()
        tat = now if row is None else row[0]
        new_tat = max(tat, now)
        allow_at = new_tat - self._window
        diff = now - allow_at
        remaining = math.floor(diff / self._emission_interval + 0.5)
        if remaining <= 0:
            retry_after = (
                -diff if remaining < 0 else self._emission_interval - diff
            )
            return RateLimitResult(
                allowed=False,
                limit=self._limit,
                remaining=0,
                retry_after=max(0.0, retry_after),
                reset_after=max(0.0, tat - now),
            )
        return RateLimitResult(
            allowed=True,
            limit=self._limit,
            remaining=remaining,
            retry_after=0.0,
            reset_after=max(0.0, new_tat - now),
        )

    async def reset(self, *, key: str) -> None:
        """Async reset (GCRA)."""
        full_key = f"{self._key_prefix}{key}"
        async with self._lock:
            await self._conn.execute(self._delete_sql, (full_key,))
