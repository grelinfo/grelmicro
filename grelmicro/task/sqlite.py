"""SQLite Job Adapter.

Every move a job makes is one `BEGIN IMMEDIATE` transaction, so it is one
atomic step for every process that opens the same file, timed by the SQLite
clock.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, NamedTuple, Self

from typing_extensions import Doc

from grelmicro._duration import MILLISECOND, round_up
from grelmicro.coordination.sqlite import _immediate
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.sqlite import SQLiteProvider
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
    from types import TracebackType

    from aiosqlite import Connection

    from grelmicro.types import BackendScope

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_SQL_NOW = (
    "SELECT CAST(round((julianday('now') - 2440587.5) * 86400000.0) "
    "AS INTEGER);"
)
"""The SQLite clock, in whole milliseconds since the epoch."""

_RECORD = (
    "id, name, status, version, done, total, result, error, "
    "cancel_requested, created_at, started_at, finished_at"
)
"""The columns a `JobRecord` is read from, in order."""

_END = """
    payload = NULL,
    context = NULL,
    version = version + 1,
    finished_at = :now,
    due_at = NULL,
    expires_at = :now + keep_ms
"""
"""What every move to an end state writes besides the status and error.

`keep_ms` is NULL for no limit, so `expires_at` is NULL then.
"""

_CATCH_UP = (
    """
    UPDATE {table_name} SET
        status = 'failed',
        error = CASE status WHEN 'running' THEN :lost ELSE :not_started END,
        runner = NULL,
    """
    + _END
    + """
    WHERE {where}
      AND status IN ('queued', 'running')
      AND due_at <= :now;
    """
)
"""End the jobs `where` selects that are past their lease or `max_wait`."""


class _Stored(NamedTuple):
    """A stored job, with what a reader never sees."""

    record: JobRecord
    owner: str | None
    runner: str | None
    started_at_submit: bool


class SQLiteJobAdapter:
    """SQLite job storage backend.

    Borrows the connection and a shared lock from a `SQLiteProvider` and
    implements the `JobBackend` protocol on a single host. Each job is one
    row of `{table_name}`, keyed by its id.

    Every call runs inside one `BEGIN IMMEDIATE` transaction, so a job's move
    and the reads that guard it apply as one step. The provider's lock
    serializes the single connection within the process, and the
    transaction's write lock serializes across processes sharing the file.
    Leases, `max_wait`, `keep` and the creation and end times follow the
    SQLite clock, read once per transaction, to the millisecond. Durations
    are rounded up to the millisecond.

    SQLite does not expire rows on its own. A read treats a job past its
    retention as gone, and `purge` deletes it.

    The first job call creates the table, so an app that registers no job
    adds nothing to the file.

    Pass an explicit `provider=` to share a connection with other components,
    or rely on the default `env_prefix=` to build one from environment
    variables.

    Example:
    ```python
    from grelmicro import Grelmicro
    from grelmicro.providers.sqlite import SQLiteProvider
    from grelmicro.task import Tasks

    tasks = Tasks()
    micro = Grelmicro(uses=[SQLiteProvider("app.db"), tasks])
    ```

    Read more in the [Task](../task.md#where-jobs-live) docs.
    """

    scope: ClassVar[BackendScope] = "host"
    """State is shared by the processes that open the same file."""

    _SQL_CREATE = (
        """
        CREATE TABLE IF NOT EXISTS {table_name} (
            seq INTEGER PRIMARY KEY,
            id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            owner TEXT,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            total INTEGER,
            result TEXT,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            payload TEXT,
            context TEXT,
            keep_ms INTEGER,
            runner TEXT,
            started_at_submit INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            due_at INTEGER,
            expires_at INTEGER
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS {table_name}_queue_idx
            ON {table_name} (name, seq) WHERE status = 'queued';
        """,
        """
        CREATE INDEX IF NOT EXISTS {table_name}_due_at_idx
            ON {table_name} (due_at) WHERE due_at IS NOT NULL;
        """,
        """
        CREATE INDEX IF NOT EXISTS {table_name}_expires_at_idx
            ON {table_name} (expires_at) WHERE expires_at IS NOT NULL;
        """,
    )
    """The table and its indexes.

    `seq` keeps submission order. `due_at` is the lease end of a running job
    and the `max_wait` end of a queued one. `expires_at` is when an ended job
    stops being kept.
    """

    _SQL: ClassVar[dict[str, str]] = {
        "catch_up": _CATCH_UP.replace("{where}", "id = :id"),
        "catch_up_queue": _CATCH_UP.replace(
            "{where}", "name = :name AND status = 'queued'"
        ),
        "reap": _CATCH_UP.replace(
            "{where}",
            """seq IN (
                SELECT seq FROM {table_name}
                WHERE status IN ('queued', 'running') AND due_at <= :now
                LIMIT :limit
            )""",
        ),
        "forget": """
            DELETE FROM {table_name} WHERE id = :id AND expires_at <= :now;
        """,
        "purge": """
            DELETE FROM {table_name} WHERE seq IN (
                SELECT seq FROM {table_name} WHERE expires_at <= :now
                LIMIT :limit
            );
        """,
        "find": """
            SELECT {record}, owner, runner, started_at_submit
            FROM {table_name} WHERE id = :id;
        """,
        "count_queued": """
            SELECT count(*) FROM {table_name}
            WHERE name = :name AND status = 'queued';
        """,
        "insert": """
            INSERT INTO {table_name} (
                id, name, owner, status, version, payload, context, keep_ms,
                runner, started_at_submit, created_at, started_at, due_at
            )
            VALUES (
                :id, :name, :owner, :status, 1, :payload, :context, :keep,
                :runner, :started, :now, :started_at, :due_at
            )
            RETURNING {record};
        """,
        "claim": """
            UPDATE {table_name} SET
                status = 'running',
                runner = :runner,
                started_at = :now,
                due_at = :now + :lease,
                version = version + 1
            WHERE seq IN (
                SELECT seq FROM {table_name}
                WHERE name = :name AND status = 'queued'
                ORDER BY seq
                LIMIT :free
            )
            RETURNING seq, {record}, payload, context;
        """,
        "beat": """
            UPDATE {table_name} SET
                due_at = :now + :lease,
                done = :done,
                total = :total,
                version = version + :changed
            WHERE id = :id;
        """,
        "settle": (
            """
            UPDATE {table_name} SET
                status = :status,
                error = :error,
                result = :result,
                done = :done,
                total = :total,
            """
            + _END
            + """
            WHERE id = :id;
            """
        ),
        "cancel_queued": (
            """
            UPDATE {table_name} SET
                status = 'cancelled',
                cancel_requested = 1,
            """
            + _END
            + """
            WHERE id = :id
            RETURNING {record};
            """
        ),
        "cancel_running": """
            UPDATE {table_name} SET
                cancel_requested = 1,
                version = version + 1
            WHERE id = :id
            RETURNING {record};
        """,
    }
    """Every statement, formatted with the table name."""

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
                """,
            ),
        ] = None,
        env_prefix: Annotated[
            str,
            Doc(
                """
                Environment variable prefix used by the implicit
                `SQLiteProvider` when `provider` is not set. Resolves
                the path from `SQLITE_PATH` by default.
                """,
            ),
        ] = "SQLITE_",
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
                and its indexes in the transaction of its first job call,
                so an app with no job adds no table. Set to False when the
                schema is managed by your own migration tool.
                """,
            ),
        ] = True,
    ) -> None:
        """Initialize the job backend."""
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", table_name):
            msg = f"Table name '{table_name}' is not a valid SQL identifier"
            raise SettingsValidationError(msg)

        if provider is None:
            self._provider = SQLiteProvider(env_prefix=env_prefix)
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
    def provider(self) -> SQLiteProvider:
        """The bound `SQLiteProvider`."""
        return self._provider

    def _rebind_provider(self, provider: SQLiteProvider) -> None:
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
        async with self._transaction() as (conn, now):
            stored = await self._find(conn, now, job_id)
            if stored is not None:
                if stored.record.name != name or stored.owner != owner:
                    return Submitted(None, started=False)
                started = (
                    stored.started_at_submit
                    and stored.record.status is JobStatus.RUNNING
                    and stored.runner == start_as
                )
                return Submitted(stored.record, started=started)
            await self._run(conn, "catch_up_queue", now, name=name)
            [queued] = await self._fetch(conn, "count_queued", name=name)
            runner = None if queued else start_as
            if (
                runner is None
                and max_queued is not None
                and queued >= max_queued
            ):
                return Submitted(None, started=False)
            if runner is None:
                due_at = None if max_wait is None else now + _ms(max_wait)
            else:
                due_at = now + _ms(lease)
            row = await self._fetch(
                conn,
                "insert",
                now=now,
                id=job_id,
                name=name,
                owner=owner,
                status=(
                    JobStatus.QUEUED if runner is None else JobStatus.RUNNING
                ).value,
                payload=payload,
                context=json.dumps(dict(context)),
                keep=None if keep is None else _ms(keep),
                runner=runner,
                started=runner is not None,
                started_at=None if runner is None else now,
                due_at=due_at,
            )
        return Submitted(_record(row), started=runner is not None)

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
                await self._run(conn, "catch_up_queue", now, name=name)
                async with conn.execute(
                    self._sql["claim"],
                    {
                        "now": now,
                        "name": name,
                        "runner": runner,
                        "lease": _ms(lease),
                        "free": slots[name],
                    },
                ) as cursor:
                    rows = sorted(await cursor.fetchall(), key=lambda r: r[0])
                claimed += [
                    Claimed(
                        _record(row[1:]),
                        payload=row[13],
                        context=json.loads(row[14]),
                    )
                    for row in rows
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
        async with self._transaction() as (conn, now):
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
                await self._run(
                    conn,
                    "beat",
                    now,
                    id=beat.job_id,
                    lease=_ms(lease),
                    done=done,
                    total=total,
                    changed=int(changed),
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
        async with self._transaction() as (conn, now):
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
            await self._run(
                conn,
                "settle",
                now,
                id=job_id,
                status=status.value,
                error=error,
                result=result,
                done=done,
                total=total,
            )
        return True

    async def get(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Return the job when `owner` owns it, else `None`."""
        async with self._transaction() as (conn, now):
            row = await self._owned(conn, now, name, job_id, owner)
        return None if row is None else row.record

    async def cancel(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Ask the job to stop when `owner` owns it, and return it."""
        async with self._transaction() as (conn, now):
            row = await self._owned(conn, now, name, job_id, owner)
            if row is None:
                return None
            record = row.record
            if record.status is JobStatus.QUEUED:
                ended = await self._fetch(
                    conn, "cancel_queued", now=now, id=job_id
                )
                return _record(ended)
            if (
                record.status is JobStatus.RUNNING
                and not record.cancel_requested
            ):
                flagged = await self._fetch(conn, "cancel_running", id=job_id)
                return _record(flagged)
        return record

    async def reap(self, *, limit: int) -> int:
        """End jobs past their lease or `max_wait`, whatever their name."""
        async with self._transaction() as (conn, now):
            return await self._run(conn, "reap", now, limit=limit)

    async def purge(self, *, limit: int) -> int:
        """Forget ended jobs past their retention, whatever their name.

        SQLite does not expire rows on its own, so this deletes up to `limit`
        of them and returns how many it deleted.
        """
        async with self._transaction() as (conn, now):
            return await self._run(conn, "purge", now, limit=limit)

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[tuple[Connection, int]]:
        """Run a body in one `BEGIN IMMEDIATE` transaction, at the SQLite time.

        The first transaction of this adapter creates the table and its
        indexes before the body, unless `auto_migrate` is off.
        """
        async with _immediate(self._provider) as conn:
            if not self._migrated:
                for sql in self._SQL_CREATE:
                    await conn.execute(sql.format(table_name=self._table_name))
            yield conn, await _now(conn)
        self._migrated = True

    async def _run(
        self,
        conn: Connection,
        key: str,
        now: int,
        **params: Any,  # noqa: ANN401
    ) -> int:
        """Run a write at `now`, and return how many rows it changed."""
        async with conn.execute(
            self._sql[key],
            {"now": now, "lost": JOB_LOST, "not_started": JOB_NOT_STARTED}
            | params,
        ) as cursor:
            return cursor.rowcount

    async def _fetch(
        self,
        conn: Connection,
        key: str,
        **params: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Run a statement, and return its one row."""
        async with conn.execute(self._sql[key], params) as cursor:
            return await cursor.fetchone()

    async def _find(
        self, conn: Connection, now: int, job_id: str
    ) -> _Stored | None:
        """Return the job as it is now, or `None` when it is gone."""
        await self._run(conn, "catch_up", now, id=job_id)
        await self._run(conn, "forget", now, id=job_id)
        row = await self._fetch(conn, "find", id=job_id)
        if row is None:
            return None
        return _Stored(_record(row), row[12], row[13], bool(row[14]))

    async def _owned(
        self,
        conn: Connection,
        now: int,
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
        now: int,
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


async def _now(conn: Connection) -> int:
    """Read the SQLite clock, in whole milliseconds since the epoch."""
    [[now]] = await conn.execute_fetchall(_SQL_NOW)
    return int(now)


def _ms(duration: timedelta) -> int:
    """Return `duration` in whole milliseconds, rounded up. Zero at the least."""
    return max(round_up(duration, MILLISECOND), 0)


def _time(value: int | None) -> datetime | None:
    """Read a stored count of milliseconds since the epoch as a datetime."""
    return None if value is None else _EPOCH + timedelta(milliseconds=value)


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
        cancel_requested=bool(row[8]),
        created_at=_EPOCH + timedelta(milliseconds=row[9]),
        started_at=_time(row[10]),
        finished_at=_time(row[11]),
    )
