"""Postgres Outbox Adapter."""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Self

import asyncpg
from typing_extensions import Doc

from grelmicro._json import json_dumps_str, json_loads
from grelmicro.outbox._message import OutboxRecord
from grelmicro.outbox._protocol import OutboxBackend
from grelmicro.outbox.errors import OutboxHandleError, OutboxTransactionError
from grelmicro.providers.postgres import PostgresProvider

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from uuid import UUID


class PostgresOutboxAdapter(OutboxBackend):
    """Postgres outbox storage backend.

    Wraps a `PostgresProvider` and implements the `OutboxBackend` protocol.
    `enqueue` runs inside the caller's transaction on an asyncpg connection
    or a SQLAlchemy session. The relay claims batches with
    `FOR UPDATE SKIP LOCKED` and a visibility lease, so every replica claims
    a disjoint set with no leader.

    Pass an explicit `provider=` to share a pool with other components, or
    rely on the default `env_prefix=` to build one from environment
    variables.
    """

    _SQL_MIGRATE = """
        CREATE TABLE IF NOT EXISTS {table} (
            id UUID PRIMARY KEY,
            topic TEXT NOT NULL,
            key TEXT,
            payload JSONB NOT NULL,
            headers JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            dedup_key TEXT,
            attempts INT NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            state TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS {table}_claim_idx
            ON {table} (available_at, id)
            WHERE state = 'pending' OR state = 'processing';
        CREATE UNIQUE INDEX IF NOT EXISTS {table}_dedup_idx
            ON {table} (dedup_key)
            WHERE dedup_key IS NOT NULL;
    """

    _SQL_ENQUEUE = """
        INSERT INTO {table}
            (id, topic, key, payload, headers, dedup_key, available_at)
        VALUES
            ($1, $2, $3, $4::jsonb, $5::jsonb, $6,
             NOW() + make_interval(secs => $7))
        ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
        RETURNING id;
    """

    _SQL_ENQUEUE_NAMED = """
        INSERT INTO {table}
            (id, topic, key, payload, headers, dedup_key, available_at)
        VALUES
            (:id, :topic, :key, CAST(:payload AS jsonb),
             CAST(:headers AS jsonb), :dedup_key,
             NOW() + make_interval(secs => :delay))
        ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
        RETURNING id;
    """

    _SQL_CLAIM = """
        UPDATE {table} SET
            state = 'processing',
            available_at = NOW() + make_interval(secs => $3),
            attempts = attempts + 1
        WHERE id IN (
            SELECT id FROM {table}
            WHERE topic = ANY($1)
              AND (state = 'pending' OR state = 'processing')
              AND available_at <= NOW()
            ORDER BY available_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        RETURNING id, topic, key, payload, headers, attempts;
    """

    _SQL_DELETE = "DELETE FROM {table} WHERE id = $1 AND attempts = $2;"

    _SQL_MARK_DELIVERED = (
        "UPDATE {table} SET state = 'delivered', last_error = NULL "
        "WHERE id = $1 AND attempts = $2;"
    )

    _SQL_RETRY = (
        "UPDATE {table} SET state = 'pending', "
        "available_at = NOW() + make_interval(secs => $2), last_error = $3 "
        "WHERE id = $1 AND attempts = $4;"
    )

    _SQL_DEAD = (
        "UPDATE {table} SET state = 'dead', last_error = $2 "
        "WHERE id = $1 AND attempts = $3;"
    )

    _SQL_REDRIVE = (
        "UPDATE {table} SET state = 'pending', available_at = NOW(), "
        "attempts = 0, last_error = NULL WHERE state = 'dead'"
    )

    _SQL_PURGE = (
        "DELETE FROM {table} "
        "WHERE (state = 'delivered' OR state = 'dead') "
        "AND ($1::float8 IS NULL "
        "OR created_at < NOW() - make_interval(secs => $1));"
    )

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
            Doc("Environment prefix for the implicit provider."),
        ] = "POSTGRES_",
        table: Annotated[
            str,
            Doc("Table that stores staged messages."),
        ] = "grelmicro_outbox",
        auto_migrate: Annotated[
            bool,
            Doc("Create the table on `__aenter__`."),
        ] = True,
        notify: Annotated[
            bool,
            Doc("Hold a LISTEN connection for low-latency wakeups."),
        ] = True,
    ) -> None:
        """Initialize the Postgres outbox backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table):
            msg = f"Table name '{table}' is not a valid SQL identifier"
            raise ValueError(msg)
        if provider is None:
            self._provider = PostgresProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._table = table
        self._auto_migrate = auto_migrate
        self._notify = notify
        self._channel = f"{table}_wake"
        self._enqueue_sql = self._SQL_ENQUEUE.format(table=table)
        self._enqueue_named_sql = self._SQL_ENQUEUE_NAMED.format(table=table)
        self._claim_sql = self._SQL_CLAIM.format(table=table)
        self._delete_sql = self._SQL_DELETE.format(table=table)
        self._delivered_sql = self._SQL_MARK_DELIVERED.format(table=table)
        self._retry_sql = self._SQL_RETRY.format(table=table)
        self._dead_sql = self._SQL_DEAD.format(table=table)
        self._redrive_sql = self._SQL_REDRIVE.format(table=table)
        self._purge_sql = self._SQL_PURGE.format(table=table)
        self._listen_conn: asyncpg.Connection[Any] | None = None
        self._listener_up = False
        self._wake = asyncio.Event()

    @property
    def provider(self) -> PostgresProvider:
        """The bound `PostgresProvider`."""
        return self._provider

    def _rebind_provider(self, provider: PostgresProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the provider, install the schema, hold the listener."""
        if self._owns_provider:
            await self._provider.__aenter__()
        try:
            if self._auto_migrate:
                await self._migrate()
            if self._notify:
                await self._open_listener()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the listener and close the provider when owned."""
        await self._close_listener()
        if self._owns_provider:
            await self._provider.__aexit__(exc_type, exc_value, traceback)

    async def _migrate(self) -> None:
        """Create the table and indexes, guarded so replicas do not race."""
        async with (
            self._provider.client.acquire() as conn,
            conn.transaction(),
        ):
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table
            )
            await conn.execute(self._SQL_MIGRATE.format(table=self._table))

    async def _open_listener(self) -> None:
        """Hold a connection that listens for wake notifications."""
        conn = await self._provider.client.acquire()
        try:
            await conn.add_listener(self._channel, self._on_notify)
        except Exception:  # pragma: no cover
            await self._provider.client.release(conn)
            raise
        conn.add_termination_listener(self._on_listener_lost)
        self._listen_conn = conn
        self._listener_up = True

    async def _close_listener(self) -> None:
        """Release the listener connection, even a terminated one.

        The reference is kept when the connection drops (see
        `_on_listener_lost`) so it is always released here. A dropped
        connection left acquired would make `pool.close()` hang on shutdown.
        """
        conn = self._listen_conn
        self._listen_conn = None
        self._listener_up = False
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.remove_listener(self._channel, self._on_notify)
        with contextlib.suppress(Exception):
            await self._provider.client.release(conn)

    def _on_notify(
        self, _conn: object, _pid: int, _channel: str, _payload: str
    ) -> None:
        """Wake the relay when a new message is signalled."""
        self._wake.set()

    def _on_listener_lost(self, _conn: object) -> None:
        """Fall back to polling when the listener connection drops.

        The connection reference is kept so `_close_listener` can still
        release it on shutdown. Polling stays the source of truth, so a
        dropped listener degrades latency until the next restart, never
        correctness.
        """
        self._listener_up = False
        self._wake.set()

    async def enqueue(self, handle: Any, record: OutboxRecord) -> bool:  # noqa: ANN401
        """Stage a record inside the caller's transaction."""
        payload = json_dumps_str(dict(record.payload))
        headers = json_dumps_str(dict(record.headers))
        delay = _available_in_seconds(record)
        if isinstance(handle, asyncpg.Pool):
            msg = (
                "publish needs a connection inside a transaction, not a pool. "
                "A pool hands out a fresh connection, so the message would "
                "land in a separate transaction."
            )
            raise OutboxHandleError(msg)
        if hasattr(handle, "is_in_transaction") and hasattr(handle, "fetchval"):
            return await self._enqueue_asyncpg(
                handle, record, payload, headers, delay
            )
        return await self._enqueue_sqlalchemy(
            handle, record, payload, headers, delay
        )

    async def _enqueue_asyncpg(
        self,
        conn: asyncpg.Connection[Any],
        record: OutboxRecord,
        payload: str,
        headers: str,
        delay: float,
    ) -> bool:
        """Insert on an asyncpg connection already in a transaction."""
        if not conn.is_in_transaction():
            raise OutboxTransactionError(_TXN_MESSAGE)
        inserted_id = await conn.fetchval(
            self._enqueue_sql,
            record.id,
            record.topic,
            record.key,
            payload,
            headers,
            record.dedup_key,
            delay,
        )
        if inserted_id is not None and self._notify:
            await conn.execute("SELECT pg_notify($1, '')", self._channel)
        return inserted_id is not None

    async def _enqueue_sqlalchemy(
        self,
        session: Any,  # noqa: ANN401
        record: OutboxRecord,
        payload: str,
        headers: str,
        delay: float,
    ) -> bool:
        """Insert on a SQLAlchemy session or connection in a transaction.

        Accepts an async `AsyncSession` or `AsyncConnection` and their
        subclasses, so SQLModel's `AsyncSession` works too. A sync `Session`
        is rejected, since its `execute` is not awaitable.
        """
        try:
            from sqlalchemy import text  # noqa: PLC0415
            from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
                AsyncConnection,
                AsyncSession,
            )
        except ImportError:  # pragma: no cover
            is_async_session = False
        else:
            is_async_session = isinstance(
                session, (AsyncSession, AsyncConnection)
            )
        if not is_async_session:
            msg = (
                "publish needs an asyncpg connection or a SQLAlchemy "
                f"AsyncSession in a transaction, got {type(session).__name__}."
            )
            raise OutboxHandleError(msg)

        if not session.in_transaction():
            raise OutboxTransactionError(_TXN_MESSAGE)
        # SQLModel overrides the session run method to nudge toward its typed
        # query helper, which does not apply to a Core statement. Bind
        # SQLAlchemy's own method so SQLModel users are not warned on every
        # publish. For a plain session or connection this is identical.
        run = (
            partial(AsyncSession.execute, session)
            if isinstance(session, AsyncSession)
            else session.execute
        )
        result = await run(
            text(self._enqueue_named_sql),
            {
                "id": record.id,
                "topic": record.topic,
                "key": record.key,
                "payload": payload,
                "headers": headers,
                "dedup_key": record.dedup_key,
                "delay": delay,
            },
        )
        inserted = result.first() is not None
        if inserted and self._notify:
            await run(
                text("SELECT pg_notify(:channel, '')"),
                {"channel": self._channel},
            )
        return inserted

    async def claim(
        self, *, topics: Sequence[str], limit: int, lease: float
    ) -> list[OutboxRecord]:
        """Claim up to `limit` due messages for the given topics."""
        if not topics:
            return []
        rows = await self._provider.client.fetch(
            self._claim_sql, list(topics), limit, lease
        )
        return [
            OutboxRecord(
                id=row["id"],
                topic=row["topic"],
                key=row["key"],
                payload=_load_object(row["payload"]),
                headers=_load_object(row["headers"]),
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def complete(
        self, *, message_id: UUID, attempts: int, keep: bool
    ) -> None:
        """Mark a message delivered by deleting or flagging its row."""
        sql = self._delivered_sql if keep else self._delete_sql
        await self._provider.client.execute(sql, message_id, attempts)

    async def reschedule(
        self,
        *,
        message_id: UUID,
        attempts: int,
        delay: float,
        error: str,
        dead: bool,
    ) -> None:
        """Reschedule a failed message or dead-letter it."""
        if dead:
            await self._provider.client.execute(
                self._dead_sql, message_id, error, attempts
            )
        else:
            await self._provider.client.execute(
                self._retry_sql, message_id, delay, error, attempts
            )

    async def redrive(self, *, topic: str | None = None) -> int:
        """Move dead messages back to pending. Returns the count moved."""
        if topic is None:
            status = await self._provider.client.execute(self._redrive_sql)
        else:
            status = await self._provider.client.execute(
                f"{self._redrive_sql} AND topic = $1", topic
            )
        return _rows_affected(status)

    async def purge(self, *, before_seconds: float | None = None) -> int:
        """Delete delivered and dead rows. Returns the count removed."""
        status = await self._provider.client.execute(
            self._purge_sql, before_seconds
        )
        return _rows_affected(status)

    async def wait_notify(self, *, timeout: float) -> None:  # noqa: ASYNC109
        """Return when a message is signalled or `timeout` elapses."""
        if not self._listener_up:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()


_TXN_MESSAGE = (
    "publish needs a handle inside an open transaction. Wrap the call in "
    "conn.transaction() or session.begin() so the message commits with your "
    "write."
)


def _available_in_seconds(record: OutboxRecord) -> float:
    """Return the delay in seconds before a record becomes available."""
    if record.available_at is None:
        return 0.0
    now = datetime.now(UTC)
    return max(0.0, (record.available_at - now).total_seconds())


def _rows_affected(status: str) -> int:
    """Parse the row count from an asyncpg command status like 'UPDATE 3'."""
    parts = status.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


def _load_object(value: str | bytes) -> dict[str, Any]:
    """Decode a jsonb column value into a plain dict."""
    loaded = json_loads(value)
    return loaded if isinstance(loaded, dict) else {}
