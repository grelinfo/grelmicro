"""SQLite circuit-breaker adapter."""

from __future__ import annotations

import asyncio
import re
from time import time
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience._protocol import (
    CircuitBreakerBackend,
    CircuitBreakerSnapshot,
    CircuitBreakerStrategy,
)
from grelmicro.resilience.circuitbreaker import CircuitBreakerState

if TYPE_CHECKING:
    from types import TracebackType

    import aiosqlite

    from grelmicro.resilience.circuitbreaker.consecutive_count import (
        ConsecutiveCountConfig,
    )


class SQLiteCircuitBreakerAdapter(CircuitBreakerBackend):
    """SQLite circuit breaker adapter.

    Builds a per-breaker
    [`CircuitBreakerStrategy`][grelmicro.resilience.CircuitBreakerStrategy]
    that stores state in a row of `{table_name}` keyed by breaker name.
    Every admission and counter update runs as a read-modify-write inside
    a `BEGIN IMMEDIATE` transaction, which takes the write lock up front,
    guarded by the provider's lock that serializes the single connection
    within the process. Processes sharing the file converge on the same
    state.

    The breaker coordinates state for processes sharing one SQLite file
    on a single host. SQLite is a local file, so it does not span hosts.
    For fleet-wide state use
    [`RedisCircuitBreakerAdapter`][grelmicro.resilience.RedisCircuitBreakerAdapter]
    or
    [`PostgresCircuitBreakerAdapter`][grelmicro.resilience.PostgresCircuitBreakerAdapter].

    Today the consecutive-count algorithm is the only strategy. Future
    algorithm configs plug in through the same `bind` entry point.

    `last_error` and `last_error_time` stay per-replica.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.sqlite import SQLiteProvider
    from grelmicro.resilience import CircuitBreakers, CircuitBreaker

    sqlite = SQLiteProvider("app.db")
    micro = Grelmicro(uses=[sqlite, CircuitBreakers(sqlite)])
    payments = CircuitBreaker("payments")
    ```

    Read more in the [Circuit Breaker](../resilience/circuit-breaker.md) docs.
    """

    is_shared: ClassVar[bool] = True

    _SQL_CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            name TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'CLOSED',
            opened_at REAL NOT NULL DEFAULT 0,
            cool_down REAL NOT NULL DEFAULT 0,
            cerr INTEGER NOT NULL DEFAULT 0,
            csucc INTEGER NOT NULL DEFAULT 0,
            ho_admit INTEGER NOT NULL DEFAULT 0
        );
    """

    _KEY_PREFIX = "cb:"

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
                Prefix prepended to every breaker name the adapter
                writes. Use it to avoid collisions with other consumers
                of the same SQLite table.
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
                on `__aenter__`. Set to False when the schema is
                managed by your own migration tool.
                """
            ),
        ] = True,
    ) -> None:
        """Initialize the circuit breaker adapter."""
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
        self._key_prefix = f"{prefix}{self._KEY_PREFIX}"
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
        *,
        name: str,
        config: ConsecutiveCountConfig,
    ) -> CircuitBreakerStrategy:
        """Build a strategy for the named breaker and config.

        Dispatches on the `config.kind` discriminator. Today only
        `consecutive_count` is supported.
        """
        if config.kind == "consecutive_count":
            return _SQLiteConsecutiveCountStrategy(
                conn=self._provider.client,
                lock=self._provider.connection_lock,
                name=f"{self._key_prefix}{name}",
                table_name=self._table_name,
                config=config,
            )
        msg = f"Unsupported circuit breaker algorithm: {config.kind!r}"
        raise NotImplementedError(msg)


class _SQLiteConsecutiveCountStrategy(CircuitBreakerStrategy):
    """SQLite consecutive-count strategy.

    Ports the Postgres PL/pgSQL state machine to Python. Each method
    runs its read, counter update, and any state transition inside a
    single `BEGIN IMMEDIATE` transaction guarded by the provider lock,
    so the whole step applies atomically across processes sharing the
    file.
    """

    _SQL_SELECT = (
        "SELECT state, opened_at, cool_down, cerr, csucc, ho_admit "
        "FROM {table_name} WHERE name = ?;"
    )
    _SQL_UPSERT = """
        INSERT INTO {table_name}
            (name, state, opened_at, cool_down, cerr, csucc, ho_admit)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET
            state = excluded.state,
            opened_at = excluded.opened_at,
            cool_down = excluded.cool_down,
            cerr = excluded.cerr,
            csucc = excluded.csucc,
            ho_admit = excluded.ho_admit;
    """

    def __init__(
        self,
        *,
        conn: aiosqlite.Connection,
        lock: asyncio.Lock,
        name: str,
        table_name: str,
        config: ConsecutiveCountConfig,
    ) -> None:
        """Bind the strategy to the breaker's name and config."""
        self._conn = conn
        self._lock = lock
        self._name = name
        self._error_threshold = config.error_threshold
        self._success_threshold = config.success_threshold
        self._reset_timeout = config.reset_timeout
        self._half_open_capacity = config.half_open_capacity
        self._select_sql = self._SQL_SELECT.format(table_name=table_name)
        self._upsert_sql = self._SQL_UPSERT.format(table_name=table_name)

    async def _read(self) -> _Row:
        async with self._conn.execute(
            self._select_sql, (self._name,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return _Row()
        return _Row(
            state=CircuitBreakerState(row[0]),
            opened_at=float(row[1]),
            cool_down=float(row[2]),
            cerr=int(row[3]),
            csucc=int(row[4]),
            ho_admit=int(row[5]),
        )

    async def _write(self, row: _Row) -> None:
        await self._conn.execute(
            self._upsert_sql,
            (
                self._name,
                row.state.value,
                row.opened_at,
                row.cool_down,
                row.cerr,
                row.csucc,
                row.ho_admit,
            ),
        )

    async def try_acquire(self) -> bool:
        """Atomic admission inside a write transaction."""
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE;")
            try:
                result = await self._try_acquire()
                await self._conn.execute("COMMIT;")
            except BaseException:
                await self._conn.execute("ROLLBACK;")
                raise
        return result

    async def _try_acquire(self) -> bool:
        row = await self._read()
        if row.state in (
            CircuitBreakerState.CLOSED,
            CircuitBreakerState.FORCED_CLOSED,
        ):
            return True
        if row.state == CircuitBreakerState.FORCED_OPEN:
            return False
        if row.state == CircuitBreakerState.OPEN:
            if time() >= row.opened_at + row.cool_down:
                row.state = CircuitBreakerState.HALF_OPEN
                row.opened_at = 0.0
                row.cool_down = 0.0
                row.cerr = 0
                row.csucc = 0
                row.ho_admit = 0
                await self._write(row)
            else:
                return False
        if (
            row.state == CircuitBreakerState.HALF_OPEN
            and row.ho_admit < self._half_open_capacity
        ):
            row.ho_admit += 1
            await self._write(row)
            return True
        return False

    async def record_outcome(
        self,
        *,
        success: bool,
        duration: float = 0.0,  # noqa: ARG002
    ) -> CircuitBreakerSnapshot:
        """Atomic outcome record with conditional state transition."""
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE;")
            try:
                snapshot = await self._record_outcome(success=success)
                await self._conn.execute("COMMIT;")
            except BaseException:
                await self._conn.execute("ROLLBACK;")
                raise
        return snapshot

    async def _record_outcome(self, *, success: bool) -> CircuitBreakerSnapshot:
        row = await self._read()
        if row.state in (
            CircuitBreakerState.FORCED_OPEN,
            CircuitBreakerState.FORCED_CLOSED,
            CircuitBreakerState.OPEN,
        ):
            return _snapshot_of(row)

        if success:
            row.csucc += 1
            row.cerr = 0
            if row.state == CircuitBreakerState.HALF_OPEN:
                if row.ho_admit > 0:  # pragma: no branch
                    row.ho_admit -= 1
                if row.csucc >= self._success_threshold:
                    row.state = CircuitBreakerState.CLOSED
                    row.opened_at = 0.0
                    row.cool_down = 0.0
                    row.cerr = 0
                    row.csucc = 0
                    row.ho_admit = 0
        else:
            row.cerr += 1
            row.csucc = 0
            if row.state == CircuitBreakerState.HALF_OPEN and row.ho_admit > 0:
                row.ho_admit -= 1
            if row.cerr >= self._error_threshold:
                row.state = CircuitBreakerState.OPEN
                row.opened_at = time()
                row.cool_down = self._reset_timeout
                row.cerr = 0
                row.csucc = 0
                row.ho_admit = 0

        await self._write(row)
        return _snapshot_of(row)

    async def transition(
        self,
        *,
        desired: CircuitBreakerState,
        cool_down: float | None = None,
    ) -> None:
        """Manual transition. Last-write-wins."""
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE;")
            try:
                row = _Row(state=desired)
                if desired == CircuitBreakerState.OPEN:
                    row.opened_at = time()
                    row.cool_down = (
                        cool_down
                        if cool_down is not None
                        else self._reset_timeout
                    )
                await self._write(row)
                await self._conn.execute("COMMIT;")
            except BaseException:
                await self._conn.execute("ROLLBACK;")
                raise

    async def get_snapshot(self) -> CircuitBreakerSnapshot:
        """Read the current snapshot."""
        async with self._lock:
            row = await self._read()
        return _snapshot_of(row)


class _Row:
    """Mutable in-memory copy of a breaker row.

    `bind`-time defaults match the `CLOSED` virtual state used when the
    row is absent.
    """

    __slots__ = (
        "cerr",
        "cool_down",
        "csucc",
        "ho_admit",
        "opened_at",
        "state",
    )

    def __init__(
        self,
        *,
        state: CircuitBreakerState = CircuitBreakerState.CLOSED,
        opened_at: float = 0.0,
        cool_down: float = 0.0,
        cerr: int = 0,
        csucc: int = 0,
        ho_admit: int = 0,
    ) -> None:
        self.state = state
        self.opened_at = opened_at
        self.cool_down = cool_down
        self.cerr = cerr
        self.csucc = csucc
        self.ho_admit = ho_admit


def _snapshot_of(row: _Row) -> CircuitBreakerSnapshot:
    return CircuitBreakerSnapshot(
        state=row.state,
        opened_at=row.opened_at,
        consecutive_error_count=row.cerr,
        consecutive_success_count=row.csucc,
    )
