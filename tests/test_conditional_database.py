"""The three ways to back a conditional write, each proved against a database.

[Conditional Requests](../docs/http/conditional.md) says a conditional
`UPDATE` on a version column, a `SELECT ... FOR UPDATE`, and a distributed
`ReadWriteLock` all work behind the same `If-Match`, and names the first as
the one to reach for. A page that says so without a test is a page a reader
disproves on their first afternoon.

Each test drives the race the strategy exists to lose safely: two writers
that read the same version, where exactly one may win and the other has to
reach the client as `412`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiosqlite
import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError
from testcontainers.postgres import PostgresContainer

from grelmicro import Grelmicro
from grelmicro.coordination import ReadWriteLock
from grelmicro.http import (
    ConditionalRequests,
    ErrorResponses,
    PreconditionFailedError,
    check_precondition,
)
from grelmicro.providers.memory import MemoryProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = [pytest.mark.timeout(60)]

HTTP_200_OK = 200
HTTP_412_PRECONDITION_FAILED = 412
FIRST_VERSION = 1
SECOND_VERSION = 2


# --- Strategy 1: a conditional UPDATE on a version column ----------------


async def _sqlite_cart(path: Path) -> str:
    """Create a cart row at version 1 and return the database path."""
    database = str(path / "carts.db")
    async with aiosqlite.connect(database) as db:
        await db.execute(
            "CREATE TABLE carts (id INTEGER PRIMARY KEY, items TEXT, "
            "version INTEGER NOT NULL)"
        )
        await db.execute(
            "INSERT INTO carts VALUES (1, 'apple', :version)",
            {"version": FIRST_VERSION},
        )
        await db.commit()
    return database


async def _conditional_update(
    database: str, *, items: str, expected: int
) -> None:
    """Write only if the row still carries the version the client held.

    The whole conflict detection is `rowcount == 0`: the statement either
    matched the version or changed nothing, decided by the database in one
    round trip with no lock held anywhere.
    """
    async with aiosqlite.connect(database) as db:
        cursor = await db.execute(
            "UPDATE carts SET items = :items, version = version + 1 "
            "WHERE id = 1 AND version = :expected",
            {"items": items, "expected": expected},
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise PreconditionFailedError


async def test_a_conditional_update_lets_one_writer_win(
    tmp_path: Path,
) -> None:
    """Both read version 1, both write, and the second is refused."""
    # Arrange
    database = await _sqlite_cart(tmp_path)

    # Act
    await _conditional_update(database, items="pear", expected=FIRST_VERSION)

    # Assert
    with pytest.raises(PreconditionFailedError):
        await _conditional_update(
            database, items="plum", expected=FIRST_VERSION
        )
    async with (
        aiosqlite.connect(database) as db,
        db.execute("SELECT items, version FROM carts") as cursor,
    ):
        assert await cursor.fetchone() == ("pear", SECOND_VERSION)


async def test_the_lost_race_reaches_the_client_as_412(
    tmp_path: Path,
) -> None:
    """The check passes, another writer lands, and the write still refuses.

    This is the window `check_precondition` cannot close, and the reason
    the write itself has to be conditional. The client sees one answer for
    both, because it is one thing: the resource moved.
    """
    # Arrange
    database = await _sqlite_cart(tmp_path)
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        async with (
            aiosqlite.connect(database) as db,
            db.execute("SELECT version FROM carts") as cursor,
        ):
            row = await cursor.fetchone()
        version = row[0] if row else 0
        # The client is current as far as this handler can tell.
        check_precondition(version)
        # A competing writer lands right here, between the check and the
        # write, which is exactly what the conditional UPDATE catches.
        await _conditional_update(database, items="plum", expected=version)
        return {"version": version + 1}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.put("/carts/1", headers={"If-Match": '"1"'})
        second = client.put("/carts/1", headers={"If-Match": '"1"'})

    # Assert
    assert first.status_code == HTTP_200_OK
    # The second request read version 2, so its own check passed, and the
    # stale If-Match is what the guard caught.
    assert second.status_code == HTTP_412_PRECONDITION_FAILED
    assert second.json()["type"].endswith("#precondition-failed")


# --- Strategy 1, through the SQLAlchemy ORM ------------------------------


class _Base(DeclarativeBase):
    """Declarative base for the versioned mapping under test."""


class _Cart(_Base):
    """A row whose version SQLAlchemy maintains and checks."""

    __tablename__ = "orm_carts"

    id: Mapped[int] = mapped_column(primary_key=True)
    items: Mapped[str]
    version: Mapped[int] = mapped_column(nullable=False)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


@pytest.fixture
async def orm_sessions(tmp_path: Path) -> AsyncIterator[Any]:
    """Yield a session factory over a SQLite file holding one cart."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/orm.db")
    async with engine.begin() as connection:
        await connection.run_sync(_Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(_Cart(id=1, items="apple"))
        await session.commit()
    yield sessions
    await engine.dispose()


async def test_sqlalchemy_raises_stale_data_for_the_second_writer(
    orm_sessions: Any,  # noqa: ANN401
) -> None:
    """`version_id_col` writes the same conditional UPDATE for you.

    It reports the empty result as `StaleDataError`, which the handler maps
    to the same rejection the raw statement raises itself.
    """
    # Arrange
    async with orm_sessions() as first, orm_sessions() as second:
        mine = await first.get(_Cart, 1)
        theirs = await second.get(_Cart, 1)

        # Act
        mine.items = "pear"
        await first.commit()

        # Assert
        theirs.items = "plum"
        with pytest.raises(StaleDataError):
            await second.commit()


# --- Strategy 2: SELECT ... FOR UPDATE -----------------------------------


@pytest.mark.integration
async def test_select_for_update_serializes_the_read_modify_write() -> None:
    """The second transaction waits, then sees the version it must refuse.

    Postgres, because a row lock is what this strategy is: SQLite has no
    `FOR UPDATE` to take.
    """
    # Arrange
    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        dsn = f"postgresql://test:test@localhost:{port}/test"
        import asyncpg  # noqa: PLC0415

        setup = await asyncpg.connect(dsn)
        await setup.execute(
            "CREATE TABLE carts (id int primary key, items text, "
            "version int not null)"
        )
        await setup.execute(
            "INSERT INTO carts VALUES (1, 'apple', $1)", FIRST_VERSION
        )
        await setup.close()
        refused: list[bool] = []

        async def write(items: str, expected: int, hold: float) -> None:
            """Take the row lock, check the version, then write under it."""
            connection = await asyncpg.connect(dsn)
            try:
                async with connection.transaction():
                    version = await connection.fetchval(
                        "SELECT version FROM carts WHERE id = 1 FOR UPDATE"
                    )
                    await anyio.sleep(hold)
                    # Inside the lock, so nothing can move between the
                    # check and the write.
                    if version != expected:
                        refused.append(True)
                        return
                    await connection.execute(
                        "UPDATE carts SET items = $1, version = version + 1 "
                        "WHERE id = 1",
                        items,
                    )
            finally:
                await connection.close()

        # Act
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(write, "pear", FIRST_VERSION, 0.2)
            await anyio.sleep(0.05)
            tasks.start_soon(write, "plum", FIRST_VERSION, 0.0)

        # Assert
        check = await asyncpg.connect(dsn)
        try:
            row = await check.fetchrow("SELECT items, version FROM carts")
        finally:
            await check.close()
        assert refused == [True]
        assert (row["items"], row["version"]) == ("pear", SECOND_VERSION)


# --- Strategy 3: a distributed ReadWriteLock ------------------------------


async def test_a_readwritelock_closes_the_window_without_a_row() -> None:
    """For state that is not a row, the lock is what orders the writers.

    The second writer enters only after the first left, reads the version
    it moved to, and refuses instead of overwriting it.
    """
    # Arrange
    micro = Grelmicro(uses=[MemoryProvider()])
    store = {"version": FIRST_VERSION, "items": "apple"}
    refused: list[bool] = []

    async def write(items: str, expected: int) -> None:
        """Read, check and save, alone."""
        async with ReadWriteLock("cart:1").write:
            if store["version"] != expected:
                refused.append(True)
                return
            await anyio.sleep(0.05)
            store["items"] = items
            store["version"] += 1

    # Act
    async with micro, anyio.create_task_group() as tasks:
        tasks.start_soon(write, "pear", FIRST_VERSION)
        await anyio.sleep(0.01)
        tasks.start_soon(write, "plum", FIRST_VERSION)

    # Assert
    assert refused == [True]
    assert (store["items"], store["version"]) == ("pear", SECOND_VERSION)


async def test_the_guard_reads_the_request_inside_a_task_group() -> None:
    """A check moved onto a child task still sees the request it belongs to.

    Every strategy above may run the check inside a nursery or a
    transaction helper, so the binding has to travel with the task rather
    than sit on the connection. It does, because a child task starts from
    a copy of its parent's context.

    What does not travel is the exception: a task group raises an
    `ExceptionGroup`, which no framework handler matches, so a check on a
    child task has to be unwrapped before it leaves the handler.
    """
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(_check_in_child)
        except* PreconditionFailedError as group:
            raise group.exceptions[0] from None
        return {"version": SECOND_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        stale = client.put("/carts/1", headers={"If-Match": '"9"'})
        current = client.put("/carts/1", headers={"If-Match": '"1"'})

    # Assert
    assert stale.status_code == HTTP_412_PRECONDITION_FAILED
    assert current.status_code == HTTP_200_OK


async def _check_in_child() -> None:
    """Run the precondition check from a child task."""
    check_precondition(FIRST_VERSION)
