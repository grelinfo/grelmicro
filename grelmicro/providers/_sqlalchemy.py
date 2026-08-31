"""Pool-shaped facade over a SQLAlchemy `AsyncEngine`.

Adapters call the provider's client as an `asyncpg.Pool`: they acquire a
connection, open a transaction on it, and run statements written with
asyncpg's positional placeholders. `EnginePool` answers that same surface
by borrowing a connection from the engine's pool and unwrapping it to the
asyncpg connection underneath, so every statement runs unchanged and the
database sees one pool.
"""

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import TYPE_CHECKING, Any, cast

from asyncpg import InterfaceError

from grelmicro.errors import SettingsValidationError

logger = getLogger("grelmicro.providers")

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from asyncpg import Connection, Record
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


_DRIVER = "asyncpg"
_BACKEND = "postgresql"


def validate_engine(engine: object) -> AsyncEngine:
    """Return the engine when it can serve asyncpg connections.

    Raises:
        SettingsValidationError: When the object is not an `AsyncEngine`, or
            when its dialect is not `postgresql+asyncpg`.
    """
    try:
        from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
            AsyncConnection,
            AsyncEngine,
            AsyncSession,
        )
    except ImportError as error:  # pragma: no cover
        msg = (
            "engine: SQLAlchemy is not installed. "
            "Install it to build a provider from an engine."
        )
        raise SettingsValidationError(msg) from error

    if isinstance(engine, (AsyncSession, AsyncConnection)):
        msg = (
            f"engine: input should be an AsyncEngine, got "
            f"{type(engine).__name__}. A session or a connection carries the "
            f"transaction you have open, and grelmicro would write inside it. "
            f"Pass the engine, or your session to outbox.publish()."
        )
        raise SettingsValidationError(msg)
    if not isinstance(engine, AsyncEngine):
        msg = (
            f"engine: input should be an AsyncEngine, got "
            f"{type(engine).__name__}."
        )
        raise SettingsValidationError(msg)
    if engine.dialect.name != _BACKEND:
        msg = f"engine: backend should be {_BACKEND!r}, got {engine.dialect.name!r}"
        raise SettingsValidationError(msg)
    if engine.dialect.driver != _DRIVER:
        msg = f"engine: driver should be {_DRIVER!r}, got {engine.dialect.driver!r}"
        raise SettingsValidationError(msg)
    return engine


class _Acquire:
    """Stand in for `Pool.acquire()`, awaited or entered.

    asyncpg returns one object that serves both call styles, and adapters
    use both: a lock takes the connection inside `async with`, while the
    outbox listener awaits one and holds it until shutdown.
    """

    __slots__ = ("_conn", "_pool")

    def __init__(self, pool: EnginePool) -> None:
        self._pool = pool
        self._conn: Connection | None = None

    def __await__(self) -> Generator[Any, None, Connection]:
        """Check a connection out and hand it to the caller to release."""
        return self._pool.checkout().__await__()

    async def __aenter__(self) -> Connection:
        """Check a connection out for the length of the block."""
        self._conn = await self._pool.checkout()
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        """Give the connection back to the engine's pool."""
        conn, self._conn = self._conn, None
        if conn is not None:
            await self._pool.release(conn)


class EnginePool:
    """The `asyncpg.Pool` surface adapters use, served by an `AsyncEngine`.

    Each checkout takes a fresh connection from the engine's pool rather
    than reusing one the application holds, so a statement grelmicro
    commits is never rolled back by a transaction the caller opened.
    """

    __slots__ = ("_borrowed", "_closed", "_engine")

    def __init__(self, engine: AsyncEngine) -> None:
        """Initialize the facade over an already validated engine."""
        self._engine = engine
        self._borrowed: dict[Connection, AsyncConnection] = {}
        self._closed = False

    async def checkout(self) -> Connection:
        """Borrow a connection from the engine and unwrap it to asyncpg.

        Raises:
            InterfaceError: When the pool has been closed, matching what
                a closed `asyncpg.Pool` raises.
        """
        if self._closed:
            msg = "pool is closed"
            raise InterfaceError(msg)
        connection = await self._engine.connect().start()
        try:
            raw = await connection.get_raw_connection()
            driver = cast("Connection", raw.driver_connection)
        except BaseException:
            await connection.close()
            raise
        self._borrowed[driver] = connection
        return driver

    async def release(self, conn: Connection) -> None:
        """Clean a borrowed connection and give it back to the engine.

        The connection goes back into the application's pool, so what
        grelmicro left on it is undone first: an open transaction, a
        `LISTEN`, or an open cursor would otherwise be found by whichever
        part of the application checks it out next.

        Only grelmicro's own leftovers are cleared. `RESET ALL` is not
        run, because the application sets its session state once per
        physical connection and never again: a `search_path` or a
        `statement_timeout` it applied on connect has to survive the
        loan.

        A connection that cannot be cleaned is invalidated rather than
        pooled, so a broken one is never handed to the application.
        """
        connection = self._borrowed.pop(conn, None)
        if connection is None:
            return
        # Hand the work to a task and keep waiting through repeated
        # cancellations. asyncio.shield only protects the inner task from
        # the awaiter's cancel, and under a cancel scope that re-delivers,
        # every await in the cleanup would raise straight away and strand
        # the checkout for the garbage collector to drop.
        task = asyncio.ensure_future(self._release(conn, connection))
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:  # pragma: no cover
                cancelled = error
        if cancelled is not None:  # pragma: no cover
            raise cancelled

    async def _release(
        self, conn: Connection, connection: AsyncConnection
    ) -> None:
        """Clean the connection, then close or invalidate the checkout."""
        try:
            await self._clean(conn)
        except asyncio.CancelledError:
            await self._discard(connection)
            raise
        except Exception:
            logger.warning(
                "Could not clean a Postgres connection, dropping it instead "
                "of returning it to the engine.",
                exc_info=True,
            )
            await self._discard(connection)
        else:
            await connection.close()

    async def _clean(self, conn: Connection) -> None:
        """Undo what grelmicro leaves on a connection, and nothing else."""
        statements = ["UNLISTEN *", "CLOSE ALL"]
        if conn.is_in_transaction():
            statements.insert(0, "ROLLBACK")
        await conn.execute("; ".join(statements))

    async def _discard(self, connection: AsyncConnection) -> None:
        """Invalidate a connection so the engine never pools it again."""
        try:
            await connection.invalidate()
        finally:
            await connection.close()

    def acquire(self) -> _Acquire:
        """Borrow a connection, awaited or entered as a context manager."""
        return _Acquire(self)

    async def execute(self, query: str, *args: Any) -> str:  # noqa: ANN401
        """Run a statement on a borrowed connection."""
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def executemany(
        self, query: str, args: Sequence[Sequence[Any]]
    ) -> None:
        """Run a statement once per argument set on a borrowed connection."""
        async with self.acquire() as conn:
            await conn.executemany(query, args)

    async def fetch(self, query: str, *args: Any) -> list[Record]:  # noqa: ANN401
        """Return every row on a borrowed connection."""
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Record | None:  # noqa: ANN401
        """Return the first row on a borrowed connection."""
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:  # noqa: ANN401
        """Return the first column of the first row on a borrowed connection."""
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def release_all(self) -> None:
        """Give every outstanding connection back, keeping the engine open.

        One connection that refuses to close never strands the rest: this
        runs from the provider's shutdown, where a raise would also
        replace whatever error was already on its way out.
        """
        while self._borrowed:
            conn = next(iter(self._borrowed))
            try:
                await self.release(conn)
            except Exception:
                logger.warning(
                    "Could not return a Postgres connection to the engine.",
                    exc_info=True,
                )

    async def close(self) -> None:
        """Give every outstanding connection back and dispose the engine."""
        self._closed = True
        await self.release_all()
        await self._engine.dispose()
