"""Postgres Job Adapter.

Every move a job makes is one `READ COMMITTED` transaction, so it is one
atomic step for every replica that connects to the same database, timed by
the Postgres clock.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, NamedTuple, Self

from typing_extensions import Doc

from grelmicro._duration import microseconds
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.task._protocol import (
    JOB_LOST,
    JOB_NOT_STARTED,
    Beat,
    BeatReply,
    Claimed,
    EndStatus,
    JobRecord,
    JobStatus,
    ProgressUpdate,
    Submitted,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from datetime import datetime
    from types import TracebackType

    from asyncpg import Connection

    from grelmicro.types import BackendScope

_JOB_ADVISORY_NAMESPACE = 0x67726C6A_6F627321
"""Advisory-lock namespace for the queue of one job name.

`hashtextextended` is the Postgres 64-bit text hash with a seed. A distinct
seed gives job names their own 64-bit lock id space, isolated from any other
advisory lock in the same database.
"""

_RECORD = (
    "id, name, status, version, done, total, result, error, "
    "cancel_requested, created_at, started_at, finished_at"
)
"""The columns a `JobRecord` is read from, in order."""


def _end(now: str) -> str:
    """Return what every move to an end state writes besides the status.

    `now` is the parameter that holds the transaction's time. `keep_us` is
    NULL for no limit, so `expires_at` is NULL then. A count of microseconds
    times a one-microsecond interval adds elapsed time only, never calendar
    days, so the session time zone plays no part.
    """
    return f"""
        payload = NULL,
        context = NULL,
        version = version + 1,
        finished_at = {now}::timestamptz,
        due_at = NULL,
        expires_at = {now}::timestamptz
            + keep_us * INTERVAL '1 microsecond'
    """


_CATCH_UP = (
    """
    UPDATE {table_name} SET
        status = 'failed',
        error = CASE status WHEN 'running' THEN $2::text ELSE $3::text END,
        runner = NULL,
    """
    + _end("$1")
    + """
    WHERE {where}
      AND status IN ('queued', 'running')
      AND due_at <= $1::timestamptz;
    """
)
"""End the jobs `where` selects that are past their lease or `max_wait`.

`$1` is the time, `$2` and `$3` the errors, `$4` what `where` reads.
"""


class _Stored(NamedTuple):
    """A stored job, with what a reader never sees."""

    record: JobRecord
    owner: str | None
    runner: str | None
    started_at_submit: bool


_TABLE_NAME_MAX = 48
"""The longest table name whose index names stay within 63 bytes.

Postgres cuts an identifier at 63 bytes, and the longest index suffix,
`_expires_at_idx`, takes 15 of them.
"""


class PostgresJobAdapter:
    """Postgres job storage backend.

    Wraps a `PostgresProvider` and implements the `JobBackend` protocol
    across every replica that connects to the same database. Each job is
    one row of `{table_name}`, keyed by its id.

    Every call runs in one `READ COMMITTED` transaction. A call on one job
    first locks its row, then reads the Postgres clock once, so the guard
    and the write apply as one step, and every time it writes follows the
    move it waited for. Claims, `reap` and `purge` take rows with
    `FOR UPDATE SKIP LOCKED`, so they never wait on another move, and two
    concurrent claims never take the same job. A claim takes only jobs
    submitted before its clock read.
    A `submit` with `max_queued` holds a transaction advisory lock on the
    job name, so the count and the insert apply as one step. Leases,
    `max_wait`, `keep` and the creation and end times follow the Postgres
    clock, to the microsecond.

    Postgres does not expire rows on its own. A read treats a job past its
    retention as gone, and `purge` deletes it.

    The first job call creates the table, so an app that registers no job
    adds nothing to the database.

    Pass an explicit `provider=` to share a pool with other components, or
    rely on the default `env_prefix=` to build one from environment
    variables.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.postgres import PostgresProvider
    from grelmicro.task import Tasks

    tasks = Tasks()
    micro = Grelmicro(
        uses=[PostgresProvider("postgresql://localhost/app"), tasks]
    )
    ```

    Read more in the [Task](../task.md#where-jobs-live) docs.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    _SQL_CREATE = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            seq BIGINT GENERATED ALWAYS AS IDENTITY,
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner TEXT,
            status TEXT NOT NULL,
            version BIGINT NOT NULL,
            done BIGINT NOT NULL DEFAULT 0,
            total BIGINT,
            result TEXT,
            error TEXT,
            cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
            payload TEXT,
            context TEXT,
            keep_us BIGINT,
            runner TEXT,
            started_at_submit BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            due_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS {table_name}_queue_idx
            ON {table_name} (name, seq) WHERE status = 'queued';
        CREATE INDEX IF NOT EXISTS {table_name}_due_at_idx
            ON {table_name} (due_at) WHERE due_at IS NOT NULL;
        CREATE INDEX IF NOT EXISTS {table_name}_expires_at_idx
            ON {table_name} (expires_at) WHERE expires_at IS NOT NULL;
    """
    """The table and its indexes.

    `seq` keeps submission order. `due_at` is the lease end of a running job
    and the `max_wait` end of a queued one. `expires_at` is when an ended job
    stops being kept.
    """

    _SQL: ClassVar[dict[str, str]] = {
        "lock": """
            SELECT 1 FROM {table_name}
            WHERE id = ANY($1::text[])
            ORDER BY id
            FOR UPDATE;
        """,
        "lock_name": (
            "SELECT pg_advisory_xact_lock("
            f"hashtextextended($1::text, {_JOB_ADVISORY_NAMESPACE}));"
        ),
        "now": "SELECT clock_timestamp();",
        "catch_up": _CATCH_UP.replace("{where}", "id = $4::text"),
        "catch_up_queue": _CATCH_UP.replace(
            "{where}",
            """id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE name = $4::text
                  AND status = 'queued'
                  AND due_at <= $1::timestamptz
                FOR UPDATE SKIP LOCKED
            ))""",
        ),
        "reap": _CATCH_UP.replace(
            "{where}",
            """id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE status IN ('queued', 'running')
                  AND due_at <= $1::timestamptz
                LIMIT $4::bigint
                FOR UPDATE SKIP LOCKED
            ))""",
        ),
        "forget": """
            DELETE FROM {table_name}
            WHERE id = $1::text AND expires_at <= $2::timestamptz;
        """,
        "purge": """
            DELETE FROM {table_name} WHERE id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE expires_at <= $1::timestamptz
                LIMIT $2::bigint
                FOR UPDATE SKIP LOCKED
            ));
        """,
        "find": """
            SELECT {record}, owner, runner, started_at_submit
            FROM {table_name} WHERE id = $1::text;
        """,
        "count_queued": """
            SELECT count(*) FROM {table_name}
            WHERE name = $1::text
              AND status = 'queued'
              AND (due_at IS NULL OR due_at > $2::timestamptz);
        """,
        "insert": """
            INSERT INTO {table_name} (
                id, name, owner, status, version, payload, context, keep_us,
                runner, started_at_submit, created_at, started_at, due_at
            )
            VALUES (
                $1::text, $2::text, $3::text, $4::text, 1, $5::text,
                $6::text, $7::bigint, $8::text, $9::boolean,
                $10::timestamptz, $11::timestamptz, $12::timestamptz
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING {record};
        """,
        "claim": """
            UPDATE {table_name} SET
                status = 'running',
                runner = $3::text,
                started_at = $1::timestamptz,
                due_at = $4::timestamptz,
                version = version + 1
            WHERE status = 'queued' AND id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE name = $2::text
                  AND status = 'queued'
                  AND created_at <= $1::timestamptz
                  AND (due_at IS NULL OR due_at > $1::timestamptz)
                ORDER BY seq
                LIMIT $5::bigint
                FOR UPDATE SKIP LOCKED
            ))
            RETURNING seq, {record}, payload, context;
        """,
        "beat": """
            UPDATE {table_name} SET
                due_at = $2::timestamptz,
                done = $3::bigint,
                total = $4::bigint,
                version = version + $5::bigint
            WHERE id = $1::text;
        """,
        "settle": (
            """
            UPDATE {table_name} SET
                status = $2::text,
                error = $3::text,
                result = $4::text,
                done = $5::bigint,
                total = $6::bigint,
            """
            + _end("$7")
            + """
            WHERE id = $1::text;
            """
        ),
        "cancel_queued": (
            """
            UPDATE {table_name} SET
                status = 'cancelled',
                cancel_requested = TRUE,
            """
            + _end("$2")
            + """
            WHERE id = $1::text
            RETURNING {record};
            """
        ),
        "cancel_running": """
            UPDATE {table_name} SET
                cancel_requested = TRUE,
                version = version + 1
            WHERE id = $1::text
            RETURNING {record};
        """,
    }
    """Every statement, formatted with the table name."""

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
            str,
            Doc(
                """
                Table that stores the jobs. Auto-created by the first
                job call (set `auto_migrate=False` to opt out).
                """,
            ),
        ] = "grelmicro_jobs",
        auto_migrate: Annotated[
            bool,
            Doc(
                """
                When True (the default), the adapter creates the table
                and its indexes before its first job call, so an app with
                no job adds no table. Set to False when the schema is
                managed by your own migration tool.
                """,
            ),
        ] = True,
    ) -> None:
        """Initialize the job backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise SettingsValidationError(msg)
        if len(table_name) > _TABLE_NAME_MAX:
            msg = (
                f"Table name '{table_name}' is longer than {_TABLE_NAME_MAX} "
                "characters, so Postgres would cut its index names"
            )
            raise SettingsValidationError(msg)

        if provider is None:
            self._provider = PostgresProvider(env_prefix=env_prefix)
            self._owns_provider = True
        else:
            self._provider = provider
            self._owns_provider = False
        self._env_prefix = env_prefix
        self._table_name = table_name
        self._migrated = not auto_migrate
        self._sql = {
            key: template.format(table_name=table_name, record=_RECORD)
            for key, template in self._SQL.items()
        }

    @property
    def provider(self) -> PostgresProvider:
        """The bound `PostgresProvider`."""
        return self._provider

    def _rebind_provider(self, provider: PostgresProvider) -> None:
        """Swap the underlying provider (used by `Grelmicro` for sharing)."""
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        """Open the provider when owned."""
        if self._owns_provider:
            await self._provider.__aenter__()
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

    async def submit(
        self,
        *,
        name: str,
        job_id: str,
        owner: str | None,
        payload: str,
        context: Mapping[str, str],
        max_queued: int | None,
        max_wait: timedelta | None,
        keep: timedelta | None,
        start_as: str | None,
        lease: timedelta,
    ) -> Submitted:
        """Store a job, or refuse it."""
        async with self._transaction(
            ids=[job_id], name=None if max_queued is None else name
        ) as (conn, began):
            now = began
            while True:
                stored = await self._find(conn, now, job_id)
                if stored is not None:
                    return _retried(stored, name, owner, start_as)
                await self._run(conn, "catch_up_queue", now, name)
                queued = await conn.fetchval(
                    self._sql["count_queued"], name, now
                )
                runner = None if queued else start_as
                if (
                    runner is None
                    and max_queued is not None
                    and queued >= max_queued
                ):
                    return Submitted(None, started=False)
                if runner is None:
                    due_at = None if max_wait is None else now + _span(max_wait)
                else:
                    due_at = now + _span(lease)
                row = await conn.fetchrow(
                    self._sql["insert"],
                    job_id,
                    name,
                    owner,
                    (
                        JobStatus.QUEUED
                        if runner is None
                        else JobStatus.RUNNING
                    ).value,
                    payload,
                    json.dumps(dict(context)),
                    None if keep is None else _us(keep),
                    runner,
                    runner is not None,
                    now,
                    None if runner is None else now,
                    due_at,
                )
                if row is not None:
                    return Submitted(_record(row), started=runner is not None)
                await conn.execute(self._sql["lock"], [job_id])
                now = await conn.fetchval(self._sql["now"])

    async def claim(
        self,
        *,
        slots: Mapping[str, int],
        runner: str,
        lease: timedelta,
    ) -> list[Claimed]:
        """Claim queued jobs up to each name's slots, oldest first per name."""
        names = [name for name, free in slots.items() if free > 0]
        if not names:
            return []
        claimed: list[Claimed] = []
        async with self._transaction() as (conn, now):
            for name in names:
                await self._run(conn, "catch_up_queue", now, name)
                rows = await conn.fetch(
                    self._sql["claim"],
                    now,
                    name,
                    runner,
                    now + _span(lease),
                    slots[name],
                )
                claimed += [
                    Claimed(
                        _record(tuple(row)[1:]),
                        payload=row[13],
                        context=json.loads(row[14]),
                    )
                    for row in sorted(rows, key=lambda r: r[0])
                ]
        return claimed

    async def beat(
        self,
        *,
        runner: str,
        beats: Sequence[Beat],
        lease: timedelta,
    ) -> list[BeatReply]:
        """Extend each held job's lease, write its progress, read its flag."""
        if not beats:
            return []
        replies: list[BeatReply] = []
        async with self._transaction(ids=[b.job_id for b in beats]) as (
            conn,
            now,
        ):
            for beat in beats:
                row = await self._held(
                    conn, now, beat.name, beat.job_id, runner
                )
                if row is None:
                    replies.append(
                        BeatReply(
                            beat.name,
                            beat.job_id,
                            held=False,
                            cancel_requested=False,
                        )
                    )
                    continue
                done, total = row.record.done, row.record.total
                progress = beat.progress
                changed = progress is not None and (done, total) != (
                    progress.done,
                    progress.total,
                )
                if progress is not None:
                    done, total = progress.done, progress.total
                await conn.execute(
                    self._sql["beat"],
                    beat.job_id,
                    now + _span(lease),
                    done,
                    total,
                    int(changed),
                )
                replies.append(
                    BeatReply(
                        beat.name,
                        beat.job_id,
                        held=True,
                        cancel_requested=row.record.cancel_requested,
                    )
                )
        return replies

    async def settle(
        self,
        *,
        name: str,
        job_id: str,
        runner: str,
        status: EndStatus,
        error: str | None,
        result: str | None,
        progress: ProgressUpdate | None,
    ) -> bool:
        """End a running job, and start its retention."""
        async with self._transaction(ids=[job_id]) as (conn, now):
            row = await self._find(conn, now, job_id)
            if row is None or row.record.name != name or row.runner != runner:
                return False
            if row.record.status is not JobStatus.RUNNING:
                return row.record.status is status
            done, total = (
                (row.record.done, row.record.total)
                if progress is None
                else (progress.done, progress.total)
            )
            await conn.execute(
                self._sql["settle"],
                job_id,
                status.value,
                error,
                result,
                done,
                total,
                now,
            )
        return True

    async def get(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Return the job when `owner` owns it, else `None`."""
        async with self._transaction(ids=[job_id]) as (conn, now):
            row = await self._owned(conn, now, name, job_id, owner)
        return None if row is None else row.record

    async def cancel(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Ask the job to stop when `owner` owns it, and return it."""
        async with self._transaction(ids=[job_id]) as (conn, now):
            row = await self._owned(conn, now, name, job_id, owner)
            if row is None:
                return None
            record = row.record
            if record.status is JobStatus.QUEUED:
                ended = await conn.fetchrow(
                    self._sql["cancel_queued"], job_id, now
                )
                return _record(ended)
            if (
                record.status is JobStatus.RUNNING
                and not record.cancel_requested
            ):
                flagged = await conn.fetchrow(
                    self._sql["cancel_running"], job_id
                )
                return _record(flagged)
        return record

    async def reap(self, *, limit: int) -> int:
        """End jobs past their lease or `max_wait`, whatever their name."""
        async with self._transaction() as (conn, now):
            return await self._run(conn, "reap", now, limit)

    async def purge(self, *, limit: int) -> int:
        """Forget ended jobs past their retention, whatever their name.

        Postgres does not expire rows on its own, so this deletes up to
        `limit` of them and returns how many it deleted.
        """
        async with self._transaction() as (conn, now):
            return _rows_affected(
                await conn.execute(self._sql["purge"], now, limit)
            )

    @asynccontextmanager
    async def _transaction(
        self, *, ids: Sequence[str] = (), name: str | None = None
    ) -> AsyncGenerator[tuple[Connection, datetime]]:
        """Run a body in one transaction, at the Postgres time.

        The transaction first takes the advisory lock of `name`, when set,
        then the row lock of each job of `ids` in id order, and only then
        reads the clock. A move that waited for another one therefore never
        writes a time before it. `READ COMMITTED` gives each statement a
        fresh snapshot, so a read after a lock sees what the holder wrote.

        Before its first transaction the adapter creates the table and its
        indexes, unless `auto_migrate` is off.
        """
        async with self._provider.client.acquire() as conn:
            if not self._migrated:
                await self._migrate(conn)
            async with conn.transaction(isolation="read_committed"):
                if name is not None:
                    await conn.execute(self._sql["lock_name"], name)
                if ids:
                    await conn.execute(self._sql["lock"], list(ids))
                yield conn, await conn.fetchval(self._sql["now"])

    async def _migrate(self, conn: Connection) -> None:
        """Create the table and indexes, guarded so replicas do not race."""
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table_name
            )
            await conn.execute(
                self._SQL_CREATE.format(table_name=self._table_name)
            )
        self._migrated = True

    async def _run(
        self,
        conn: Connection,
        key: str,
        now: datetime,
        arg: Any,  # noqa: ANN401
    ) -> int:
        """Run a catch-up write at `now`, and return how many rows it ended."""
        return _rows_affected(
            await conn.execute(
                self._sql[key], now, JOB_LOST, JOB_NOT_STARTED, arg
            )
        )

    async def _find(
        self, conn: Connection, now: datetime, job_id: str
    ) -> _Stored | None:
        """Return the job as it is now, or `None` when it is gone."""
        await self._run(conn, "catch_up", now, job_id)
        await conn.execute(self._sql["forget"], job_id, now)
        row = await conn.fetchrow(self._sql["find"], job_id)
        if row is None:
            return None
        return _Stored(_record(row), row[12], row[13], row[14])

    async def _owned(
        self,
        conn: Connection,
        now: datetime,
        name: str,
        job_id: str,
        owner: str | None,
    ) -> _Stored | None:
        """Return the job when it has this name and `owner` owns it."""
        row = await self._find(conn, now, job_id)
        if row is None or row.record.name != name or row.owner != owner:
            return None
        return row

    async def _held(
        self,
        conn: Connection,
        now: datetime,
        name: str,
        job_id: str,
        runner: str,
    ) -> _Stored | None:
        """Return the running job when `runner` holds it."""
        row = await self._find(conn, now, job_id)
        if (
            row is None
            or row.record.name != name
            or row.record.status is not JobStatus.RUNNING
            or row.runner != runner
        ):
            return None
        return row


def _retried(
    stored: _Stored, name: str, owner: str | None, start_as: str | None
) -> Submitted:
    """Answer a `submit` whose id is already stored."""
    if stored.record.name != name or stored.owner != owner:
        return Submitted(None, started=False)
    started = (
        stored.started_at_submit
        and stored.record.status is JobStatus.RUNNING
        and stored.runner == start_as
    )
    return Submitted(stored.record, started=started)


def _us(duration: timedelta) -> int:
    """Return `duration` in whole microseconds. Zero at the least."""
    return max(microseconds(duration), 0)


def _span(duration: timedelta) -> timedelta:
    """Return `duration`, or zero when it is negative."""
    return max(duration, timedelta(0))


def _rows_affected(status: str) -> int:
    """Parse the row count from an asyncpg command status like 'UPDATE 3'."""
    parts = status.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


def _record(row: Sequence[Any]) -> JobRecord:
    """Build a `JobRecord` from the `_RECORD` columns that start `row`."""
    return JobRecord(
        id=row[0],
        name=row[1],
        status=JobStatus(row[2]),
        version=row[3],
        done=row[4],
        total=row[5],
        result=row[6],
        error=row[7],
        cancel_requested=row[8],
        created_at=row[9],
        started_at=row[10],
        finished_at=row[11],
    )
