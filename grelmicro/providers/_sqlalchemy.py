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

_CLEAN_TIMEOUT = 10.0
"""Seconds a connection gets to answer the cleanup before it is dropped.

A server that accepts the socket and then stops answering would otherwise
hold the release open, and the release runs where a caller cannot cancel
it.
"""


async def _close(connection: AsyncConnection) -> None:
    """Return a checkout to the engine, reporting a refusal rather than raising.

    A release runs from `__aexit__`, so an error raised here would replace
    whatever the caller's block was already raising.
    """
    try:
        await connection.close()
    except BaseException:
        # Including a cancel. This is the last step that puts the checkout
        # back, so letting one through here is the leak the shielded task
        # exists to prevent.
        logger.warning(
            "Could not return a Postgres connection to the engine.",
            exc_info=True,
        )


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


def _forget_transaction(conn: Connection) -> bool:
    """Clear asyncpg's record of the transaction it was starting.

    asyncpg notes the transaction before it sends `BEGIN`, so a cancel in
    between leaves the note with no transaction open. Left there, the next
    `transaction()` on this connection reads as a nested one and sends
    `SAVEPOINT` outside a transaction block.

    Returns whether a note was found, so the caller rolls back with it.
    """
    try:
        if conn._top_xact is None:  # noqa: SLF001
            return False
        conn._top_xact = None  # noqa: SLF001
    except AttributeError:
        _report_driver_change("transaction")
        return False
    return True


def _forget_listeners(conn: Connection) -> None:
    """Clear asyncpg's record of the channels it believes are subscribed.

    `add_listener` skips the round trip for a channel already in the
    record, so one left behind would make a later `LISTEN` silently do
    nothing.
    """
    try:
        conn._listeners.clear()  # noqa: SLF001
        conn._log_listeners.clear()  # noqa: SLF001
    except AttributeError:
        _report_driver_change("listener")


_reported_driver_changes: set[str] = set()
"""Records already reported missing, so the warning is said once."""


def _report_driver_change(what: str) -> None:
    """Say that asyncpg no longer keeps a record where grelmicro looked.

    These records are private to asyncpg, and a release that renames one
    must not turn every check-in into a dropped connection. The server-side
    cleanup still runs, so the connection goes back either way.

    Said once per record. Every release would otherwise report it, which is
    once per request for an app that takes one lock.
    """
    if what in _reported_driver_changes:
        return
    _reported_driver_changes.add(what)
    logger.warning(
        "This asyncpg keeps no %s record where grelmicro looks for one, so "
        "it cannot be cleared on the client. Upgrade grelmicro if a newer "
        "one supports this asyncpg.",
        what,
    )


class _Acquire:
    """Stand in for `Pool.acquire()`, awaited or entered.

    asyncpg returns one object that serves both call styles, and adapters
    use both. The two are not equivalent here: a block that borrows a
    connection cannot outlive its own statements, while a caller that
    awaits one holds it and may add a listener to it, so only the awaited
    form needs the full clean on the way back.
    """

    __slots__ = ("_conn", "_pool")

    def __init__(self, pool: EnginePool) -> None:
        self._pool = pool
        self._conn: Connection | None = None

    def __await__(self) -> Generator[Any, None, Connection]:
        """Check a connection out and hand it to the caller to release."""
        return self._pool.checkout(held=True).__await__()

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

    __slots__ = ("_borrowed", "_closed", "_engine", "_held", "_releasing")

    def __init__(self, engine: AsyncEngine) -> None:
        """Initialize the facade over an already validated engine."""
        self._engine = engine
        self._borrowed: dict[Connection, AsyncConnection] = {}
        self._held: set[Connection] = set()
        self._releasing: set[asyncio.Task[None]] = set()
        self._closed = False

    async def checkout(self, *, held: bool = False) -> Connection:
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
            if self._closed:
                # `close()` drained the checkouts while this one was still
                # opening, so it would never be cleaned or given back.
                msg = "pool is closed"
                raise InterfaceError(msg)  # noqa: TRY301
        except BaseException:
            await _close(connection)
            raise
        self._borrowed[driver] = connection
        if held:
            self._held.add(driver)
        return driver

    async def release(self, conn: Connection, *, discard: bool = False) -> None:
        """Clean a borrowed connection and give it back to the engine.

        The connection goes back into the application's pool, so what
        grelmicro left on it is undone first. Only grelmicro's own
        leftovers are cleared: `RESET ALL` is never run, because the
        application sets its session state once per physical connection
        and never again, so a `search_path` or a `statement_timeout` it
        applied on connect has to survive the loan.

        Nothing is sent when there is nothing to undo, which is the
        common case: a statement run through this pool leaves no
        transaction and no listener, and both checks are answered from
        the client without a round trip.

        A connection that cannot be cleaned is invalidated rather than
        pooled, so a broken one is never handed to the application.
        """
        connection = self._borrowed.pop(conn, None)
        held = conn in self._held
        self._held.discard(conn)
        if connection is None:
            return
        # Hand the work to a task and keep waiting through repeated
        # cancellations. asyncio.shield only protects the inner task from
        # the awaiter's cancel, and under a cancel scope that re-delivers,
        # every await in the cleanup would raise straight away and strand
        # the checkout for the garbage collector to drop.
        task = asyncio.ensure_future(
            self._release(conn, connection, held=held, discard=discard)
        )
        self._releasing.add(task)
        task.add_done_callback(self._releasing.discard)
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:  # pragma: no cover
                cancelled = error
        if task.cancelled():
            # The cleanup itself was cancelled, which says the connection
            # went away, not that the caller did. It has been dropped
            # already, and reporting a cancel here would cancel siblings
            # of a caller nobody asked to stop.
            logger.warning(
                "The cleanup of a Postgres connection was cancelled, so the "
                "connection was dropped instead of returned to the engine."
            )
        # A caller that is itself being cancelled still has to see it, even
        # when the cleanup task was cancelled in the same breath, which is
        # what the loop does when it tears every task down at once.
        current = asyncio.current_task()
        if cancelled is not None and (
            not task.cancelled()
            or (current is not None and current.cancelling())
        ):
            raise cancelled

    async def _release(
        self,
        conn: Connection,
        connection: AsyncConnection,
        *,
        held: bool,
        discard: bool = False,
    ) -> None:
        """Clean the connection, then close or invalidate the checkout."""
        if discard:
            await self._discard(connection)
            return
        try:
            async with asyncio.timeout(_CLEAN_TIMEOUT):
                await self._clean(conn, held=held)
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
            await _close(connection)

    async def _clean(self, conn: Connection, *, held: bool) -> None:
        """Undo what grelmicro leaves on a connection, and nothing else.

        A connection the caller held may carry a listener, so it is told
        to forget them. One borrowed for the length of a block cannot,
        and pays no round trip for the question.

        The two markers asyncpg keeps on the client are cleared the way
        its own pool clears them, because the server answering `ROLLBACK`
        and `UNLISTEN` does not tell the driver anything.
        """
        statements = []
        # `_forget_transaction` is what clears the marker, so it runs before
        # the test rather than behind an `or` that skips it exactly when the
        # connection is in a transaction.
        forgotten = _forget_transaction(conn)
        if conn.is_in_transaction() or forgotten:
            statements.append("ROLLBACK")
        if held:
            _forget_listeners(conn)
            statements += ["UNLISTEN *", "CLOSE ALL"]
        if statements:
            await conn.execute("; ".join(statements))

    async def _discard(self, connection: AsyncConnection) -> None:
        """Invalidate a connection so the engine never pools it again."""
        try:
            await connection.invalidate()
        except BaseException:
            # Including a cancel: the checkout still has to go back, which
            # is the whole reason this runs inside a shielded task.
            logger.warning(
                "Could not invalidate a Postgres connection.", exc_info=True
            )
        await _close(connection)

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
        replace whatever error was already on its way out. A cancel is
        the exception, and is re-raised once every connection is back.
        """
        if self._borrowed:
            logger.warning(
                "Dropping %d Postgres connection(s) still checked out of the "
                "engine. A component held one past its own shutdown, and its "
                "connection is not safe to hand back while it may still be "
                "running statements on it.",
                len(self._borrowed),
            )
        cancelled: asyncio.CancelledError | None = None
        while self._borrowed:
            conn = next(iter(self._borrowed))
            try:
                # Whoever holds this never gave it back, so it may still be
                # driving it. Handing it to the application now would put two
                # coroutines on one connection.
                await self.release(conn, discard=True)
            except asyncio.CancelledError as error:  # pragma: no cover
                cancelled = error
            except Exception:
                logger.warning(
                    "Could not return a Postgres connection to the engine.",
                    exc_info=True,
                )
        if cancelled is not None:  # pragma: no cover
            raise cancelled

    async def close(self) -> None:
        """Give every outstanding connection back and dispose the engine.

        A release already in flight has popped its connection out of the
        record, so it is waited on by its task rather than found in there.
        Disposing the engine under it would fail its cleanup and cost the
        application a connection.
        """
        self._closed = True
        await self.release_all()
        if self._releasing:
            await asyncio.gather(*self._releasing, return_exceptions=True)
        await self._engine.dispose()
