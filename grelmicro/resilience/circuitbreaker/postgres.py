"""Postgres circuit-breaker adapter."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from logging import getLogger
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._duration import microseconds
from grelmicro.coordination._base import jittered_interval
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSnapshot,
    CircuitBreakerStrategy,
)
from grelmicro.resilience.circuitbreaker import (
    _STATE_TTL,
    _STATE_TTL_RESET_FACTOR,
    CircuitBreakerState,
)

logger = getLogger("grelmicro")

_CLEANUP_LIMIT = 100
"""Rows a single sweep may delete, bounding its cost."""

_CLEANUP_FIRST_DELAY = 30.0
"""Seconds before the first sweep, so short-lived workers reclaim too."""

_CLEANUP_JITTER = 0.2
"""Interval jitter, so replicas do not sweep in lockstep."""

if TYPE_CHECKING:
    from datetime import timedelta
    from types import TracebackType

    from asyncpg import Pool

    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )
    from grelmicro.types import BackendScope


_CIRCUIT_BREAKER_ADVISORY_NAMESPACE = 0x67726362_2D636972
"""Advisory-lock namespace for the circuit breaker.

`hashtextextended` is Postgres's 64-bit text hash with a configurable
seed. A distinct seed gives breaker names their own 64-bit lock id
space, isolated from the rate limiter and any other advisory lock in
the same database.
"""


def _whole_microseconds(seconds: str) -> str:
    """Return SQL that reads a stored number of seconds in whole microseconds."""
    return f"round({seconds} * 1000000)::bigint"


def _lifetime_end(idle_us: str) -> str:
    """Return SQL for when a row's lifetime ends, in epoch microseconds.

    The lifetime is `idle_us` microseconds, or the row's cool-down times
    the floor factor when that is longer.
    """
    return (
        f"{_whole_microseconds('updated_at')} + GREATEST({idle_us}, "
        f"{_STATE_TTL_RESET_FACTOR} * {_whole_microseconds('cool_down')})"
    )


_IDLE_TTL_US = microseconds(_STATE_TTL)
"""How long a circuit nobody calls is kept, in microseconds."""

_SQL_TIME = {
    "now_us": "(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000)::bigint",
    "opened_us": _whole_microseconds("v_opened_at"),
    "cool_down_us": _whole_microseconds("v_cool_down"),
    "lifetime_end": _lifetime_end(str(_IDLE_TTL_US)),
    "sweep_end": _lifetime_end("p_idle_us"),
}
"""The time fragments a function is formatted with, in whole microseconds.

A stored time stays in seconds, and reads back exact to the microsecond.
"""


class PostgresCircuitBreakerAdapter(CircuitBreakerBackend):
    """Postgres circuit breaker adapter.

    Builds a per-breaker
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    that stores state in a row of `{table_name}` keyed by breaker name.
    Every admission and counter update runs inside a PL/pgSQL function
    that holds `pg_advisory_xact_lock` for the breaker, so concurrent
    replicas converge to the same state without coordination locks.

    Today the consecutive-count algorithm is the only strategy. Future
    algorithm configs plug in through the same `bind` entry point.

    `last_error` and `last_error_time` stay per-replica.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.postgres import PostgresProvider
    from grelmicro.resilience import CircuitBreakerComponent, CircuitBreaker
    from grelmicro.resilience.circuitbreaker.postgres import (
        PostgresCircuitBreakerAdapter,
    )

    postgres = PostgresProvider("postgresql://localhost:5432/app")
    micro = Grelmicro(
        uses=[
            postgres,
            CircuitBreakerComponent(PostgresCircuitBreakerAdapter(provider=postgres)),
        ]
    )
    payments = CircuitBreaker("payments")
    ```

    Read more in the [Circuit Breaker](../resilience/circuit-breaker.md) docs.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    is_shared: ClassVar[bool] = True

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            name TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'CLOSED',
            opened_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            cool_down DOUBLE PRECISION NOT NULL DEFAULT 0,
            cerr INT NOT NULL DEFAULT 0,
            csucc INT NOT NULL DEFAULT 0,
            ho_admit INT NOT NULL DEFAULT 0,
            updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
        );
        ALTER TABLE {table_name}
            ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION
            NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS {table_name}_updated_at_idx
            ON {table_name} (updated_at);
    """

    _SQL_CREATE_FN_TRY_ACQUIRE = """
        CREATE OR REPLACE FUNCTION {table_name}_cb_try_acquire_v2(
            p_name TEXT,
            p_capacity INT
        ) RETURNS BOOLEAN AS $$
        DECLARE
            v_now_us BIGINT := {now_us};
            v_now DOUBLE PRECISION := v_now_us / 1000000.0;
            v_state TEXT;
            v_opened_at DOUBLE PRECISION;
            v_cool_down DOUBLE PRECISION;
            v_ho_admit INT;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            DELETE FROM {table_name}
                WHERE name = p_name
                  AND state NOT IN ('FORCED_OPEN', 'FORCED_CLOSED')
                  AND {lifetime_end} <= v_now_us;
            SELECT state, opened_at, cool_down, ho_admit
                INTO v_state, v_opened_at, v_cool_down, v_ho_admit
                FROM {table_name} WHERE name = p_name;
            IF v_state IS NULL THEN
                RETURN TRUE;
            END IF;
            IF v_state = 'FORCED_CLOSED' OR v_state = 'CLOSED' THEN
                RETURN TRUE;
            END IF;
            IF v_state = 'FORCED_OPEN' THEN
                RETURN FALSE;
            END IF;
            IF v_state = 'OPEN' THEN
                IF v_now_us >= {opened_us} + {cool_down_us} THEN
                    v_state := 'HALF_OPEN';
                    v_ho_admit := 0;
                    UPDATE {table_name}
                        SET state = 'HALF_OPEN', opened_at = 0, cool_down = 0,
                            cerr = 0, csucc = 0, ho_admit = 0,
                            updated_at = v_now
                        WHERE name = p_name;
                ELSE
                    RETURN FALSE;
                END IF;
            END IF;
            IF v_state = 'HALF_OPEN' THEN
                IF v_ho_admit < p_capacity THEN
                    UPDATE {table_name}
                        SET ho_admit = ho_admit + 1, updated_at = v_now
                        WHERE name = p_name;
                    RETURN TRUE;
                END IF;
                RETURN FALSE;
            END IF;
            RETURN FALSE;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_RECORD_ERROR = """
        CREATE OR REPLACE FUNCTION {table_name}_cb_record_error_v2(
            p_name TEXT,
            p_threshold INT,
            p_reset_timeout_us BIGINT
        ) RETURNS TABLE(
            r_state TEXT, r_cerr INT, r_csucc INT,
            r_opened_at DOUBLE PRECISION, r_retry_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_state TEXT;
            v_opened_at DOUBLE PRECISION;
            v_cool_down DOUBLE PRECISION;
            v_ho_admit INT;
            v_cerr INT;
            v_now_us BIGINT := {now_us};
            v_now DOUBLE PRECISION := v_now_us / 1000000.0;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            DELETE FROM {table_name}
                WHERE name = p_name
                  AND state NOT IN ('FORCED_OPEN', 'FORCED_CLOSED')
                  AND {lifetime_end} <= v_now_us;
            SELECT t.state, t.opened_at, t.cool_down, t.ho_admit
                INTO v_state, v_opened_at, v_cool_down, v_ho_admit
                FROM {table_name} t WHERE t.name = p_name;
            IF v_state IS NULL THEN
                v_state := 'CLOSED';
                v_opened_at := 0;
                v_cool_down := 0;
                v_ho_admit := 0;
            END IF;
            IF v_state IN ('FORCED_OPEN', 'FORCED_CLOSED', 'OPEN') THEN
                RETURN QUERY SELECT v_state, 0, 0, v_opened_at,
                    {remaining};
                RETURN;
            END IF;
            INSERT INTO {table_name} (name, state, cerr, csucc, updated_at)
                VALUES (p_name, v_state, 1, 0, v_now)
                ON CONFLICT (name) DO UPDATE
                    SET cerr = {table_name}.cerr + 1, csucc = 0,
                        updated_at = v_now
                RETURNING cerr INTO v_cerr;
            IF v_state = 'HALF_OPEN' AND v_ho_admit > 0 THEN
                UPDATE {table_name}
                    SET ho_admit = ho_admit - 1, updated_at = v_now
                    WHERE name = p_name;
            END IF;
            IF v_cerr >= p_threshold THEN
                v_cool_down := p_reset_timeout_us / 1000000.0;
                UPDATE {table_name}
                    SET state = 'OPEN', opened_at = v_now,
                        cool_down = v_cool_down,
                        cerr = 0, csucc = 0, ho_admit = 0,
                        updated_at = v_now
                    WHERE name = p_name;
                RETURN QUERY SELECT 'OPEN'::TEXT, 0, 0, v_now, v_cool_down;
                RETURN;
            END IF;
            RETURN QUERY SELECT v_state, v_cerr, 0, v_opened_at,
                0::double precision;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_RECORD_SUCCESS = """
        CREATE OR REPLACE FUNCTION {table_name}_cb_record_success_v2(
            p_name TEXT,
            p_threshold INT
        ) RETURNS TABLE(
            r_state TEXT, r_cerr INT, r_csucc INT,
            r_opened_at DOUBLE PRECISION, r_retry_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_state TEXT;
            v_opened_at DOUBLE PRECISION;
            v_cool_down DOUBLE PRECISION;
            v_ho_admit INT;
            v_csucc INT;
            v_now_us BIGINT := {now_us};
            v_now DOUBLE PRECISION := v_now_us / 1000000.0;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            DELETE FROM {table_name}
                WHERE name = p_name
                  AND state NOT IN ('FORCED_OPEN', 'FORCED_CLOSED')
                  AND {lifetime_end} <= v_now_us;
            SELECT t.state, t.opened_at, t.cool_down, t.ho_admit
                INTO v_state, v_opened_at, v_cool_down, v_ho_admit
                FROM {table_name} t WHERE t.name = p_name;
            IF v_state IS NULL THEN
                v_state := 'CLOSED';
                v_opened_at := 0;
                v_cool_down := 0;
                v_ho_admit := 0;
            END IF;
            IF v_state IN ('FORCED_OPEN', 'FORCED_CLOSED', 'OPEN') THEN
                RETURN QUERY SELECT v_state, 0, 0, v_opened_at,
                    {remaining};
                RETURN;
            END IF;
            INSERT INTO {table_name} (name, state, cerr, csucc, updated_at)
                VALUES (p_name, v_state, 0, 1, v_now)
                ON CONFLICT (name) DO UPDATE
                    SET csucc = {table_name}.csucc + 1, cerr = 0,
                        updated_at = v_now
                RETURNING csucc INTO v_csucc;
            IF v_state = 'HALF_OPEN' AND v_ho_admit > 0 THEN
                UPDATE {table_name}
                    SET ho_admit = ho_admit - 1, updated_at = v_now
                    WHERE name = p_name;
            END IF;
            IF v_state = 'HALF_OPEN' AND v_csucc >= p_threshold THEN
                -- A closed circuit with cleared counters is what a
                -- missing row already means, so store nothing.
                DELETE FROM {table_name} WHERE name = p_name;
                RETURN QUERY SELECT 'CLOSED'::TEXT, 0, 0,
                    0::double precision, 0::double precision;
                RETURN;
            END IF;
            RETURN QUERY SELECT v_state, 0, v_csucc, v_opened_at,
                0::double precision;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_TRANSITION = """
        CREATE OR REPLACE FUNCTION {table_name}_cb_transition_v2(
            p_name TEXT,
            p_desired TEXT,
            p_cool_down_us BIGINT
        ) RETURNS VOID AS $$
        DECLARE
            v_now DOUBLE PRECISION := {now_us} / 1000000.0;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_name, {lock_namespace})
            );
            IF p_desired = 'CLOSED' THEN
                DELETE FROM {table_name} WHERE name = p_name;
                RETURN;
            END IF;
            IF p_desired = 'OPEN' THEN
                INSERT INTO {table_name}
                    (name, state, opened_at, cool_down, cerr, csucc, ho_admit,
                     updated_at)
                    VALUES (p_name, 'OPEN', v_now,
                            p_cool_down_us / 1000000.0, 0, 0, 0, v_now)
                    ON CONFLICT (name) DO UPDATE
                        SET state = 'OPEN', opened_at = v_now,
                            cool_down = EXCLUDED.cool_down,
                            cerr = 0, csucc = 0, ho_admit = 0,
                            updated_at = v_now;
            ELSE
                INSERT INTO {table_name}
                    (name, state, opened_at, cool_down, cerr, csucc, ho_admit,
                     updated_at)
                    VALUES (p_name, p_desired, 0, 0, 0, 0, 0, v_now)
                    ON CONFLICT (name) DO UPDATE
                        SET state = p_desired, opened_at = 0, cool_down = 0,
                            cerr = 0, csucc = 0, ho_admit = 0,
                            updated_at = v_now;
            END IF;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CREATE_FN_CLEANUP = """
        CREATE OR REPLACE FUNCTION {table_name}_cb_cleanup_v2(
            p_idle_us BIGINT,
            p_limit INT
        ) RETURNS INT AS $$
        DECLARE
            v_now_us BIGINT := {now_us};
            v_name TEXT;
            v_deleted INT := 0;
        BEGIN
            FOR v_name IN
                SELECT name FROM {table_name}
                    WHERE state NOT IN ('FORCED_OPEN', 'FORCED_CLOSED')
                      AND {sweep_end} <= v_now_us
                    LIMIT p_limit
            LOOP
                -- Skip any circuit a call is currently mutating, so the
                -- sweep can never delete a row between another
                -- transaction's read and its write.
                IF pg_try_advisory_xact_lock(
                    hashtextextended(v_name, {lock_namespace})
                ) THEN
                    DELETE FROM {table_name}
                        WHERE name = v_name
                          AND state NOT IN ('FORCED_OPEN', 'FORCED_CLOSED')
                          AND {sweep_end} <= v_now_us;
                    v_deleted := v_deleted + 1;
                END IF;
            END LOOP;
            RETURN v_deleted;
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_CLEANUP = "SELECT {table_name}_cb_cleanup_v2($1::bigint, $2);"

    _SQL_CREATE_FN_GET_STATE = """
        CREATE OR REPLACE FUNCTION {table_name}_cb_get_state_v2(p_name TEXT)
        RETURNS TABLE(
            r_state TEXT, r_cerr INT, r_csucc INT,
            r_opened_at DOUBLE PRECISION, r_retry_after DOUBLE PRECISION
        ) AS $$
        DECLARE
            v_state TEXT;
            v_cerr INT;
            v_csucc INT;
            v_opened_at DOUBLE PRECISION;
            v_cool_down DOUBLE PRECISION;
            v_now_us BIGINT := {now_us};
        BEGIN
            SELECT t.state, t.cerr, t.csucc, t.opened_at, t.cool_down
                INTO v_state, v_cerr, v_csucc, v_opened_at, v_cool_down
                FROM {table_name} t
                WHERE t.name = p_name
                  AND (
                      t.state IN ('FORCED_OPEN', 'FORCED_CLOSED')
                      OR {lifetime_end} > v_now_us
                  );
            IF v_state IS NULL THEN
                RETURN QUERY SELECT 'CLOSED'::TEXT, 0, 0,
                    0::double precision, 0::double precision;
                RETURN;
            END IF;
            RETURN QUERY SELECT v_state, v_cerr, v_csucc, v_opened_at,
                {remaining};
        END;
        $$ LANGUAGE plpgsql;
    """

    _SQL_REMAINING = (
        "(CASE WHEN v_state = 'OPEN' THEN GREATEST("
        "0, {opened_us} + {cool_down_us} - v_now_us"
        ") ELSE 0 END / 1000000.0)::double precision"
    )
    """Seconds an OPEN circuit still has to wait out.

    Evaluated inside the function, on the Postgres clock that stamped
    `opened_at`. The two clocks have no common reference from Python, so
    the subtraction cannot be done there.
    """

    _KEY_PREFIX = "cb:"

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
        prefix: Annotated[
            str,
            Doc(
                """
                Prefix prepended to every breaker name the adapter
                writes. Use it to avoid collisions with other consumers
                of the same Postgres table.
                """
            ),
        ] = "",
        table_name: Annotated[
            str,
            Doc(
                """
                Table that stores circuit-breaker state. Auto-created
                on first connect (set `auto_migrate=False` to opt out).
                """
            ),
        ] = "grelmicro_circuit_breaker",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the adapter creates the table
                and SQL functions on `__aenter__`. Set to False when
                the schema is managed by your own migration tool.
                """
            ),
        ] = True,
        cleanup_interval: Annotated[
            float | None,
            Doc(
                """
                Period in seconds between sweeps that delete circuits
                nobody has called for a day. Defaults to one hour. Pass
                `None` to disable the sweep, which leaves a dynamic key
                set to grow without bound.

                Each sweep deletes a bounded number of rows and skips
                any circuit a call is currently using, so it stays off
                the admission path.
                """
            ),
        ] = 3600.0,
    ) -> None:
        """Initialize the circuit breaker adapter."""
        if cleanup_interval is not None and cleanup_interval <= 0:
            msg = "cleanup_interval must be positive"
            raise SettingsValidationError(msg)

        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise SettingsValidationError(msg)

        if provider is None:
            self._provider = PostgresProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._prefix = prefix
        self._key_prefix = f"{prefix}{self._KEY_PREFIX}"
        self._table_name = table_name
        self._auto_migrate = auto_migrate
        self._cleanup_interval = cleanup_interval
        self._cleanup_sql = self._SQL_CLEANUP.format(table_name=table_name)
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
        """Open the adapter and install the schema when `auto_migrate=True`."""
        if self._owns_provider:
            await self._provider.__aenter__()
        self._loop = asyncio.get_running_loop()
        if self._auto_migrate:  # pragma: no branch
            await self._migrate()
        if self._cleanup_interval is not None:
            self._janitor_task = asyncio.create_task(self._janitor_loop())
        return self

    async def _migrate(self) -> None:
        """Install the schema, guarded so replicas do not race.

        `CREATE TABLE IF NOT EXISTS` checks and creates in two steps, so
        two workers starting together can both pass the check and one then
        fails on the row type the table creates. The advisory lock is held
        to the end of the transaction, so the second worker finds the
        table already there.
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
                self._SQL_CREATE_FN_TRY_ACQUIRE,
                self._SQL_CREATE_FN_RECORD_ERROR,
                self._SQL_CREATE_FN_RECORD_SUCCESS,
                self._SQL_CREATE_FN_TRANSITION,
                self._SQL_CREATE_FN_CLEANUP,
                self._SQL_CREATE_FN_GET_STATE,
            ):
                await conn.execute(
                    sql.format(
                        table_name=self._table_name,
                        lock_namespace=_CIRCUIT_BREAKER_ADVISORY_NAMESPACE,
                        remaining=self._SQL_REMAINING.format(**_SQL_TIME),
                        **_SQL_TIME,
                    )
                )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the provider when owned. External providers are left alone."""
        self._loop = None
        if self._janitor_task is not None:
            self._janitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._janitor_task
            self._janitor_task = None
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def _janitor_loop(self) -> None:
        """Sweep expired circuits on an interval until the adapter closes.

        The interval is jittered so replicas sharing one database do not
        all sweep at the same instant. The first sweep runs shortly
        after startup, so a short-lived worker still reclaims something.
        """
        interval = self._cleanup_interval or 0.0
        delay = min(_CLEANUP_FIRST_DELAY, interval)
        while True:
            await asyncio.sleep(jittered_interval(delay, _CLEANUP_JITTER))
            delay = interval
            try:
                await self._provider.client.execute(
                    self._cleanup_sql, _IDLE_TTL_US, _CLEANUP_LIMIT
                )
            except Exception:
                logger.warning(
                    "Circuit breaker cleanup sweep failed", exc_info=True
                )

    def bind(
        self,
        *,
        name: str,
        config: ConsecutiveCountConfig,
    ) -> CircuitBreakerStrategy:
        """Build a strategy for the named breaker and config.

        Dispatches on the `config.kind` discriminator. Today only
        `consecutive_count` is supported.
        """
        if config.kind == "consecutive_count":
            return _PostgresConsecutiveCountStrategy(
                pool=self._provider.client,
                name=f"{self._key_prefix}{name}",
                table_name=self._table_name,
                config=config,
            )
        msg = f"Unsupported circuit breaker algorithm: {config.kind!r}"
        raise NotImplementedError(msg)


class _PostgresConsecutiveCountStrategy(CircuitBreakerStrategy):
    """Postgres consecutive-count strategy.

    Mirrors the Redis adapter's semantics. Each method calls a PL/pgSQL
    function that holds `pg_advisory_xact_lock` for the breaker name, so
    the read, the counter update, and any state transition apply
    atomically across replicas.
    """

    _SQL_TRY_ACQUIRE = "SELECT {table_name}_cb_try_acquire_v2($1, $2);"
    _SQL_RECORD_ERROR = (
        "SELECT * FROM {table_name}_cb_record_error_v2($1, $2, $3::bigint);"
    )
    _SQL_RECORD_SUCCESS = (
        "SELECT * FROM {table_name}_cb_record_success_v2($1, $2);"
    )
    _SQL_ABANDON = """
        UPDATE {table_name}
            SET ho_admit = ho_admit - 1,
                updated_at = EXTRACT(EPOCH FROM clock_timestamp())
            WHERE name = $1 AND state = 'HALF_OPEN' AND ho_admit > 0;
    """
    _SQL_TRANSITION = (
        "SELECT {table_name}_cb_transition_v2($1, $2, $3::bigint);"
    )
    _SQL_GET_STATE = "SELECT * FROM {table_name}_cb_get_state_v2($1);"

    def __init__(
        self,
        *,
        pool: Pool,
        name: str,
        table_name: str,
        config: ConsecutiveCountConfig,
    ) -> None:
        """Bind the strategy to the breaker's name and config.

        The cool-down is passed to the functions in whole microseconds.
        """
        self._pool = pool
        self._name = name
        self._error_threshold = config.error_threshold
        self._success_threshold = config.success_threshold
        self._reset_timeout_us = microseconds(config.reset_timeout)
        self._half_open_capacity = config.half_open_capacity
        self._try_acquire_sql = self._SQL_TRY_ACQUIRE.format(
            table_name=table_name
        )
        self._record_error_sql = self._SQL_RECORD_ERROR.format(
            table_name=table_name
        )
        self._record_success_sql = self._SQL_RECORD_SUCCESS.format(
            table_name=table_name
        )
        self._abandon_sql = self._SQL_ABANDON.format(table_name=table_name)
        self._transition_sql = self._SQL_TRANSITION.format(
            table_name=table_name
        )
        self._get_state_sql = self._SQL_GET_STATE.format(table_name=table_name)

    async def try_acquire(self) -> bool:
        """Atomic admission via a PL/pgSQL function."""
        result = await self._pool.fetchval(
            self._try_acquire_sql,
            self._name,
            self._half_open_capacity,
        )
        return bool(result)

    async def abandon(self) -> None:
        """Give back a probe slot the call never used."""
        await self._pool.execute(self._abandon_sql, self._name)

    async def record_outcome(
        self,
        *,
        success: bool,
        duration: float = 0.0,  # noqa: ARG002
    ) -> CircuitBreakerSnapshot:
        """Atomic outcome record with conditional state transition."""
        if success:
            row = await self._pool.fetchrow(
                self._record_success_sql,
                self._name,
                self._success_threshold,
            )
        else:
            row = await self._pool.fetchrow(
                self._record_error_sql,
                self._name,
                self._error_threshold,
                self._reset_timeout_us,
            )
        return self._unpack(row)

    async def transition(
        self,
        *,
        desired: CircuitBreakerState,
        cool_down: timedelta | None = None,
    ) -> None:
        """Manual transition. Last-write-wins."""
        await self._pool.execute(
            self._transition_sql,
            self._name,
            desired.value,
            microseconds(cool_down)
            if cool_down is not None
            else self._reset_timeout_us,
        )

    async def get_snapshot(self) -> CircuitBreakerSnapshot:
        """Read the current snapshot."""
        row = await self._pool.fetchrow(self._get_state_sql, self._name)
        return self._unpack(row)

    @staticmethod
    def _unpack(row: Any) -> CircuitBreakerSnapshot:  # noqa: ANN401
        return CircuitBreakerSnapshot(
            state=CircuitBreakerState(row["r_state"]),
            opened_at=float(row["r_opened_at"]),
            consecutive_error_count=int(row["r_cerr"]),
            consecutive_success_count=int(row["r_csucc"]),
            retry_after=float(row["r_retry_after"]),
        )
