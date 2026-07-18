"""Postgres adapter tests: mocked-pool units and a container integration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from pydantic import BaseModel

from grelmicro.outbox import Message, Outbox
from grelmicro.outbox._message import OutboxRecord
from grelmicro.outbox._uuid import uuid7
from grelmicro.outbox.errors import OutboxHandleError, OutboxTransactionError
from grelmicro.outbox.postgres import (
    PostgresOutboxAdapter,
    _available_in_seconds,
    _load_object,
    _rows_affected,
)
from grelmicro.providers.postgres import PostgresProvider

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.timeout(60)]

URL = "postgresql://test_user:test_password@test_host:1234/test_db"


def _adapter() -> PostgresOutboxAdapter:
    """Return an adapter bound to an unopened provider (no I/O)."""
    return PostgresOutboxAdapter(provider=PostgresProvider(URL))


def test_rows_affected_parses_command_status() -> None:
    """The row count is read from an asyncpg status, else zero."""
    assert _rows_affected("UPDATE 3") == 3  # noqa: PLR2004
    assert _rows_affected("DELETE 0") == 0
    assert _rows_affected("weird") == 0


def test_load_object_falls_back_to_empty_dict() -> None:
    """A jsonb value decodes to a dict, and a non-object decodes to {}."""
    assert _load_object('{"a": 1}') == {"a": 1}
    assert _load_object("[1, 2]") == {}


def test_available_in_seconds_handles_none() -> None:
    """A record with no `available_at` is available now (zero delay)."""
    assert _available_in_seconds(_record()) == 0.0


def test_available_in_seconds_future() -> None:
    """A future `available_at` yields a positive delay."""
    record = OutboxRecord(
        id=uuid7(),
        topic="job",
        payload={},
        available_at=datetime.now(UTC) + timedelta(seconds=100),
    )
    assert _available_in_seconds(record) > 0


def _record() -> OutboxRecord:
    """Return a minimal record."""
    return OutboxRecord(id=uuid7(), topic="job", payload={"n": 1})


def test_invalid_table_name() -> None:
    """A non-identifier table name is rejected."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        PostgresOutboxAdapter(provider=PostgresProvider(URL), table="bad name")


def test_create_table_sql_renders_ddl() -> None:
    """`create_table_sql` renders the create statements for the table name."""
    sql = PostgresOutboxAdapter.create_table_sql()
    assert "CREATE TABLE IF NOT EXISTS grelmicro_outbox" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS grelmicro_outbox_dedup_idx" in sql
    custom = PostgresOutboxAdapter.create_table_sql("orders_outbox")
    assert "CREATE TABLE IF NOT EXISTS orders_outbox" in custom


def test_drop_table_sql_renders_ddl() -> None:
    """`drop_table_sql` renders the drop statement for the table name."""
    assert (
        PostgresOutboxAdapter.drop_table_sql()
        == "DROP TABLE IF EXISTS grelmicro_outbox;"
    )


@pytest.mark.parametrize("accessor", ["create_table_sql", "drop_table_sql"])
def test_ddl_accessors_reject_bad_table_name(accessor: str) -> None:
    """The DDL accessors validate the table name like the constructor."""
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        getattr(PostgresOutboxAdapter, accessor)("bad name")


async def test_enqueue_rejects_pool() -> None:
    """A pool is refused because it breaks atomicity."""
    pool = MagicMock(spec=asyncpg.Pool)
    with pytest.raises(OutboxHandleError):
        await _adapter().enqueue(pool, _record())


async def test_enqueue_rejects_unknown_handle() -> None:
    """A handle that is neither asyncpg nor SQLAlchemy is refused."""
    with pytest.raises(OutboxHandleError):
        await _adapter().enqueue(object(), _record())


async def test_enqueue_rejects_sync_sqlalchemy_session() -> None:
    """A sync SQLAlchemy `Session` is refused (its execute is not async)."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.orm import Session  # noqa: PLC0415

    with pytest.raises(OutboxHandleError):
        await _adapter().enqueue(Session(), _record())


async def test_enqueue_requires_transaction() -> None:
    """An asyncpg connection outside a transaction is refused."""
    conn = MagicMock(spec=asyncpg.Connection)
    conn.is_in_transaction.return_value = False
    with pytest.raises(OutboxTransactionError):
        await _adapter().enqueue(conn, _record())


async def test_enqueue_sqlalchemy_requires_transaction() -> None:
    """A SQLAlchemy session outside a transaction is refused."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    with pytest.raises(OutboxTransactionError):
        await _adapter().enqueue(AsyncSession(), _record())


async def test_aenter_closes_on_migrate_failure() -> None:
    """A migrate failure on enter unwinds instead of leaking."""

    class _Boom:
        async def __aenter__(self) -> None:
            msg = "acquire boom"
            raise RuntimeError(msg)

        async def __aexit__(self, *exc: object) -> None:
            return None

    provider = MagicMock()
    provider.client.acquire.return_value = _Boom()
    adapter = PostgresOutboxAdapter(provider=provider, notify=False)
    with pytest.raises(RuntimeError, match="acquire boom"):
        await adapter.__aenter__()


def test_provider_without_outbox_adapter_raises() -> None:
    """A provider that ships no outbox adapter raises a clear error."""
    from grelmicro.providers.sqlite import SQLiteProvider  # noqa: PLC0415

    with pytest.raises(NotImplementedError, match="no outbox adapter"):
        SQLiteProvider(":memory:").outbox()


def test_rebind_provider_swaps_the_pool() -> None:
    """Rebinding points the adapter at a shared provider it does not own."""
    adapter = _adapter()
    other = PostgresProvider(URL)
    adapter._rebind_provider(other)
    assert adapter.provider is other


async def test_claim_empty_topics_returns_empty() -> None:
    """Claiming with no registered topics touches no database."""
    assert await _adapter().claim(topics=[], limit=1, lease=1) == []


async def test_aenter_skips_migrate_and_listener_when_disabled() -> None:
    """With migrate and notify off, entering touches no database."""
    adapter = PostgresOutboxAdapter(
        provider=MagicMock(), auto_migrate=False, notify=False
    )
    async with adapter:
        pass


# --- Mocked-pool unit tests -------------------------------------------------


def _acquire_cm(conn: object) -> object:
    """Return an async context manager yielding `conn`."""

    class _Acquire:
        async def __aenter__(self) -> object:
            return conn

        async def __aexit__(self, *exc: object) -> None:
            return None

    return _Acquire()


def _mock_conn() -> MagicMock:
    """Return a mock asyncpg connection with an async `transaction()`."""
    conn = MagicMock()
    conn.execute = AsyncMock()

    class _Txn:
        async def __aenter__(self) -> object:
            return conn

        async def __aexit__(self, *exc: object) -> None:
            return None

    conn.transaction = _Txn
    return conn


def _mock_pool_adapter(
    *, auto_migrate: bool = True, notify: bool = True
) -> tuple[PostgresOutboxAdapter, MagicMock]:
    """Return an adapter wired to a mocked asyncpg pool via a provider."""
    provider = PostgresProvider(URL)
    pool = MagicMock()
    provider._pool = pool
    adapter = PostgresOutboxAdapter(
        provider=provider, auto_migrate=auto_migrate, notify=notify
    )
    return adapter, pool


async def test_claim_maps_rows_to_records() -> None:
    """`claim` decodes fetched rows into `OutboxRecord`s."""
    adapter, pool = _mock_pool_adapter()
    message_id = uuid7()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": message_id,
                "topic": "job",
                "key": None,
                "payload": '{"n": 1}',
                "headers": "{}",
                "attempts": 1,
            }
        ]
    )
    (record,) = await adapter.claim(topics=["job"], limit=10, lease=30)
    assert record.id == message_id
    assert record.payload == {"n": 1}
    assert record.attempts == 1


async def test_complete_deletes_or_flags() -> None:
    """`complete` deletes by default and flags when keeping."""
    adapter, pool = _mock_pool_adapter()
    pool.execute = AsyncMock()
    await adapter.complete(message_id=uuid7(), attempts=1, keep=False)
    await adapter.complete(message_id=uuid7(), attempts=1, keep=True)
    assert pool.execute.await_count == 2  # noqa: PLR2004


async def test_reschedule_retry_and_dead() -> None:
    """`reschedule` runs the retry statement or the dead statement."""
    adapter, pool = _mock_pool_adapter()
    pool.execute = AsyncMock()
    await adapter.reschedule(
        message_id=uuid7(), attempts=1, delay=5, error="e", dead=False
    )
    await adapter.reschedule(
        message_id=uuid7(), attempts=1, delay=0, error="e", dead=True
    )
    assert pool.execute.await_count == 2  # noqa: PLR2004


async def test_redrive_counts_affected_rows() -> None:
    """`redrive` returns the affected row count, with and without a topic."""
    adapter, pool = _mock_pool_adapter()
    pool.execute = AsyncMock(return_value="UPDATE 3")
    assert await adapter.redrive() == 3  # noqa: PLR2004
    assert await adapter.redrive(topic="job") == 3  # noqa: PLR2004


async def test_purge_counts_affected_rows() -> None:
    """`purge` returns the number of deleted rows."""
    adapter, pool = _mock_pool_adapter()
    pool.execute = AsyncMock(return_value="DELETE 2")
    assert await adapter.purge() == 2  # noqa: PLR2004


async def test_migrate_runs_ddl_under_advisory_lock() -> None:
    """Entering with auto_migrate runs the guarded DDL."""
    adapter, pool = _mock_pool_adapter(notify=False)
    conn = _mock_conn()
    pool.acquire = lambda: _acquire_cm(conn)
    async with adapter:
        pass
    assert conn.execute.await_count == 2  # noqa: PLR2004


async def test_listener_open_close_and_wake() -> None:
    """The listener is held on enter, woken, and released on exit."""
    adapter, pool = _mock_pool_adapter(auto_migrate=False, notify=True)
    conn = MagicMock()
    conn.add_listener = AsyncMock()
    conn.remove_listener = AsyncMock()
    conn.add_termination_listener = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    async with adapter:
        adapter._wake.set()
        await adapter.wait_notify(timeout=0.1)
    conn.add_listener.assert_awaited()
    conn.remove_listener.assert_awaited()
    pool.release.assert_awaited()


async def test_wait_notify_polls_when_listener_down() -> None:
    """With no listener, `wait_notify` falls back to a bounded sleep."""
    adapter, _ = _mock_pool_adapter(auto_migrate=False, notify=False)
    async with adapter:
        await adapter.wait_notify(timeout=0.01)


def test_notify_callbacks_wake_and_mark_down() -> None:
    """The notify and termination callbacks wake and mark the listener down."""
    adapter, _ = _mock_pool_adapter()
    adapter._on_notify(None, 1, "channel", "")
    assert adapter._wake.is_set()
    adapter._listener_up = True
    adapter._on_listener_lost(None)
    assert adapter._listener_up is False


async def test_owns_provider_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter with no explicit provider opens and closes its own."""
    monkeypatch.setenv("POSTGRES_URL", URL)
    adapter = PostgresOutboxAdapter(auto_migrate=False, notify=False)
    provider = MagicMock()
    provider.__aenter__ = AsyncMock()
    provider.__aexit__ = AsyncMock()
    adapter._provider = provider
    async with adapter:
        pass
    provider.__aenter__.assert_awaited()
    provider.__aexit__.assert_awaited()


def _sqlalchemy_session(monkeypatch: pytest.MonkeyPatch) -> object:
    """Return a bind-free SQLAlchemy AsyncSession with mocked execution."""
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    session = AsyncSession()
    monkeypatch.setattr(session, "in_transaction", lambda: True)
    result = MagicMock()
    result.first.return_value = object()
    monkeypatch.setattr(AsyncSession, "execute", AsyncMock(return_value=result))
    return session


async def test_enqueue_sqlalchemy_inserts_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SQLAlchemy publish runs the insert and the wake, and returns True."""
    pytest.importorskip("sqlalchemy")
    session = _sqlalchemy_session(monkeypatch)
    assert await _adapter().enqueue(session, _record()) is True


async def test_enqueue_sqlalchemy_skips_notify_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With notify off, a SQLAlchemy publish stages without a wake."""
    pytest.importorskip("sqlalchemy")
    session = _sqlalchemy_session(monkeypatch)
    adapter = PostgresOutboxAdapter(
        provider=PostgresProvider(URL), notify=False
    )
    assert await adapter.enqueue(session, _record()) is True


async def test_wait_notify_times_out_with_listener_up() -> None:
    """A held listener with no wake times out cleanly."""
    adapter, pool = _mock_pool_adapter(auto_migrate=False, notify=True)
    conn = MagicMock()
    conn.add_listener = AsyncMock()
    conn.remove_listener = AsyncMock()
    conn.add_termination_listener = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    async with adapter:
        await adapter.wait_notify(timeout=0.01)


async def test_enqueue_inserts_and_notifies() -> None:
    """A staged row returns True and fires a wake notification."""
    conn = MagicMock(spec=asyncpg.Connection)
    conn.is_in_transaction.return_value = True
    record = _record()
    conn.fetchval = AsyncMock(return_value=record.id)
    conn.execute = AsyncMock()
    assert await _adapter().enqueue(conn, record) is True
    conn.execute.assert_awaited()  # pg_notify


async def test_enqueue_dedup_returns_false() -> None:
    """A dedup conflict returns False and sends no notification."""
    conn = MagicMock(spec=asyncpg.Connection)
    conn.is_in_transaction.return_value = True
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    record = OutboxRecord(id=uuid7(), topic="job", payload={}, dedup_key="k")
    assert await _adapter().enqueue(conn, record) is False
    conn.execute.assert_not_awaited()


# --- Integration ---------------------------------------------------------

pg_container = pytest.importorskip("testcontainers.postgres")


class Job(BaseModel):
    """A sample payload model."""

    n: int


async def _wait(predicate: Callable[[], object], timeout: float = 10.0) -> None:  # noqa: ASYNC109
    """Poll until `predicate()` is truthy or the timeout elapses."""
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


@pytest.mark.integration
async def test_postgres_full_cycle() -> None:
    """Migrate, publish in a transaction, and let the relay deliver."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        provider = PostgresProvider(url)
        async with provider:
            outbox = Outbox(
                provider, poll_interval=0.1, retry_base=0.05, retry_jitter=0
            )
            seen: list[Message[Job]] = []

            @outbox.handler(Job)
            async def handle(message: Message[Job]) -> None:
                seen.append(message)

            async with outbox:
                async with (
                    provider.client.acquire() as conn,
                    conn.transaction(),
                ):
                    staged = await outbox.publish(conn, Job(n=1))
                    assert staged is True
                await _wait(lambda: seen)

            assert seen[0].data == Job(n=1)


@pytest.mark.integration
async def test_postgres_dead_letter_and_redrive() -> None:
    """A failing handler dead-letters, and redrive replays it."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        provider = PostgresProvider(url)
        async with provider:
            outbox = Outbox(
                provider,
                poll_interval=0.1,
                retry_base=0.02,
                retry_jitter=0,
                max_attempts=2,
            )
            calls = 0

            @outbox.handler("job")
            async def handle(message: Message[object]) -> None:  # noqa: ARG001
                nonlocal calls
                calls += 1
                msg = "boom"
                raise RuntimeError(msg)

            async with outbox:
                async with (
                    provider.client.acquire() as conn,
                    conn.transaction(),
                ):
                    await outbox.publish(conn, "job", {"n": 1})
                await _wait(lambda: calls >= 2)  # noqa: PLR2004
                await asyncio.sleep(0.5)
                assert calls == 2  # noqa: PLR2004
                assert await outbox.redrive(topic="job") == 1
                await _wait(lambda: calls >= 3)  # noqa: PLR2004


@pytest.mark.integration
async def test_adapter_owns_env_built_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit provider the adapter builds and closes its own.

    Exercises the owned-provider lifecycle and the listener open/close that
    the other tests bypass by passing a shared provider.
    """
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        monkeypatch.setenv(
            "POSTGRES_URL", f"postgresql://test:test@localhost:{port}/test"
        )
        async with PostgresOutboxAdapter() as backend:
            record = OutboxRecord(id=uuid7(), topic="job", payload={"n": 1})
            async with (
                backend.provider.client.acquire() as conn,
                conn.transaction(),
            ):
                assert await backend.enqueue(conn, record) is True
            (claimed,) = await backend.claim(topics=["job"], limit=10, lease=30)
            assert claimed.id == record.id


@pytest.mark.integration
async def test_postgres_lease_reclaim() -> None:
    """A claimed-but-unsettled message is reclaimed after its lease lapses."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        provider = PostgresProvider(url)
        async with (
            provider,
            PostgresOutboxAdapter(provider=provider, notify=False) as backend,
        ):
            record = OutboxRecord(id=uuid7(), topic="job", payload={"n": 1})
            async with (
                provider.client.acquire() as conn,
                conn.transaction(),
            ):
                await backend.enqueue(conn, record)

            (first,) = await backend.claim(topics=["job"], limit=10, lease=0.3)
            assert first.attempts == 1
            # Before the lease lapses the row stays invisible.
            assert (
                await backend.claim(topics=["job"], limit=10, lease=0.3) == []
            )
            await asyncio.sleep(0.4)
            (second,) = await backend.claim(topics=["job"], limit=10, lease=5)
            assert second.id == record.id
            assert second.attempts == 2  # noqa: PLR2004

            # Dead-letter it, then redrive across all topics (topic=None)
            # moves it back to pending.
            await backend.reschedule(
                message_id=second.id,
                attempts=second.attempts,
                delay=0,
                error="boom",
                dead=True,
            )
            assert await backend.redrive() == 1
            # Dead-letter again and purge removes the terminal row.
            (third,) = await backend.claim(topics=["job"], limit=10, lease=5)
            await backend.reschedule(
                message_id=third.id,
                attempts=third.attempts,
                delay=0,
                error="boom",
                dead=True,
            )
            assert await backend.purge() == 1
            assert await backend.claim(topics=["job"], limit=10, lease=1) == []


@pytest.mark.integration
async def test_postgres_purge_states_filter() -> None:
    """`purge(states=("delivered",))` removes delivered rows, keeps dead ones."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with (
            provider,
            PostgresOutboxAdapter(provider=provider, notify=False) as backend,
        ):

            async def _stage() -> OutboxRecord:
                record = OutboxRecord(id=uuid7(), topic="job", payload={})
                async with (
                    provider.client.acquire() as conn,
                    conn.transaction(),
                ):
                    await backend.enqueue(conn, record)
                return record

            await _stage()
            (delivered,) = await backend.claim(
                topics=["job"], limit=10, lease=30
            )
            await backend.complete(
                message_id=delivered.id, attempts=delivered.attempts, keep=True
            )

            await _stage()
            (dead,) = await backend.claim(topics=["job"], limit=10, lease=30)
            await backend.reschedule(
                message_id=dead.id,
                attempts=dead.attempts,
                delay=0,
                error="boom",
                dead=True,
            )

            # Delivered-only purge leaves the dead row for inspection.
            assert await backend.purge(states=("delivered",)) == 1
            assert await backend.purge(states=("dead",)) == 1


@pytest.mark.integration
async def test_postgres_enqueue_via_sqlalchemy() -> None:
    """Publishing on a SQLAlchemy AsyncSession joins its transaction."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
        AsyncSession,
        create_async_engine,
    )
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with provider:
            # Poll-only relay: exercises the skip-notify enqueue branch and
            # the polling wait path.
            outbox = Outbox(
                provider, poll_interval=0.1, retry_jitter=0, notify=False
            )
            seen: list[Message[object]] = []

            @outbox.handler("job")
            async def handle(message: Message[object]) -> None:
                seen.append(message)

            async with outbox:
                engine = create_async_engine(
                    f"postgresql+asyncpg://test:test@localhost:{port}/test"
                )
                try:
                    async with (
                        AsyncSession(engine) as session,
                        session.begin(),
                    ):
                        staged = await outbox.publish(session, "job", {"n": 1})
                        assert staged is True
                    await _wait(lambda: seen)
                finally:
                    await engine.dispose()

            assert seen[0].payload == {"n": 1}


@pytest.mark.integration
async def test_postgres_enqueue_via_sqlmodel() -> None:
    """Publishing on a SQLModel AsyncSession joins its transaction."""
    pytest.importorskip("sqlmodel")
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415
    from sqlmodel import Field, SQLModel  # noqa: PLC0415
    from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: PLC0415
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    class Hero(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with provider:
            outbox = Outbox(provider, poll_interval=0.1, retry_jitter=0)
            seen: list[Message[object]] = []

            @outbox.handler("hero.created")
            async def on_hero(message: Message[object]) -> None:
                seen.append(message)

            async with outbox:
                engine = create_async_engine(
                    f"postgresql+asyncpg://test:test@localhost:{port}/test"
                )
                try:
                    async with engine.begin() as conn:
                        await conn.run_sync(SQLModel.metadata.create_all)
                    async with (
                        AsyncSession(engine) as session,
                        session.begin(),
                    ):
                        session.add(Hero(name="Deadpond"))
                        staged = await outbox.publish(
                            session, "hero.created", {"name": "Deadpond"}
                        )
                        assert staged is True
                    await _wait(lambda: seen)
                finally:
                    await engine.dispose()

            assert seen[0].payload == {"name": "Deadpond"}
