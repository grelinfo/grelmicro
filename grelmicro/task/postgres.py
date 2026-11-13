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
from grelmicro.providers.postgres import (
    PostgresProvider,
    _longest_table_name,
)
from grelmicro.task._protocol import (
    FIRST,
    JOB_LOST,
    JOB_NOT_STARTED,
    JOB_TIMED_OUT,
    Beat,
    BeatReply,
    Claimed,
    EndStatus,
    JobRecord,
    JobStatus,
    Listed,
    ProgressUpdate,
    Settled,
    Submitted,
    page_of,
    read_cursor,
    scan_limit,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
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
        run_by = NULL,
        expires_at = {now}::timestamptz
            + keep_us * INTERVAL '1 microsecond'
    """


_CATCH_UP = (
    """
    UPDATE {table_name} SET
        status = 'failed',
        error = CASE
            WHEN status = 'queued' THEN $3::text
            WHEN run_by <= due_at THEN $5::text
            ELSE $2::text
        END,
        runner = NULL,
    """
    + _end("$1")
    + """
    WHERE {where}
      AND status IN ('queued', 'running')
      AND due_at <= $1::timestamptz
    {returning};
    """
)
"""End the jobs `where` selects that are past their lease, `max_wait` or
`max_run`.

`$1` is the time, `$2`, `$3` and `$5` the errors, `$4` what `where` reads. A
running job's `due_at` is the earlier of its lease end and its `run_by`, so
`run_by <= due_at` means `max_run` passed first.
"""

_CAPPED = (
    "CASE WHEN status = 'succeeded' THEN max_kept_succeeded "
    "ELSE max_kept_failed END IS NOT NULL"
)
"""Whether an ended job carries a cap of its kind, so a cap counts it."""

_ENDED = "name, status = 'succeeded', max_kept_failed"
"""What a move to `failed` returns for the cap of each job it ended."""


class _Stored(NamedTuple):
    """A stored job, with what a reader never sees."""

    record: JobRecord
    owner: str | None
    runner: str | None
    started_at_submit: bool


_TABLE_NAME_MAX = _longest_table_name(
    "_queue_idx", "_due_at_idx", "_expires_at_idx", "_owner_idx", "_kept_idx"
)
"""The longest table name that keeps every name built from it whole."""


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

    A cap on ended jobs skips a job that another call holds at that moment,
    so one job of that kind can stay past the cap until the next one ends.

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
            max_run_us BIGINT,
            max_kept_succeeded BIGINT,
            max_kept_failed BIGINT,
            runner TEXT,
            started_at_submit BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            due_at TIMESTAMPTZ,
            run_by TIMESTAMPTZ,
            expires_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS {table_name}_queue_idx
            ON {table_name} (name, seq) WHERE status = 'queued';
        CREATE INDEX IF NOT EXISTS {table_name}_due_at_idx
            ON {table_name} (due_at) WHERE due_at IS NOT NULL;
        CREATE INDEX IF NOT EXISTS {table_name}_expires_at_idx
            ON {table_name} (expires_at) WHERE expires_at IS NOT NULL;
        CREATE INDEX IF NOT EXISTS {table_name}_owner_idx
            ON {table_name} (name, owner, seq) WHERE owner IS NOT NULL;
        CREATE INDEX IF NOT EXISTS {table_name}_kept_idx
            ON {table_name} (name, (status = 'succeeded'), finished_at, seq)
            WHERE finished_at IS NOT NULL AND {capped};
    """
    """The table and its indexes.

    `seq` keeps submission order. `due_at` is the earlier of the lease end
    and the `run_by` of a running job, and the `max_wait` end of a queued
    one. `run_by` is when a running job passes its `max_run`. `expires_at`
    is when an ended job stops being kept. The owner index serves `list`,
    and the kept index the caps on ended jobs.
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
        "catch_up": _CATCH_UP.replace("{where}", "id = $4::text").replace(
            "{returning}", f"RETURNING error, {_ENDED}"
        ),
        "catch_up_queue": _CATCH_UP.replace(
            "{returning}", f"RETURNING {_ENDED}"
        ).replace(
            "{where}",
            """id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE name = $4::text
                  AND status = 'queued'
                  AND due_at <= $1::timestamptz
                FOR UPDATE SKIP LOCKED
            ))""",
        ),
        "lock_owner": """
            SELECT 1 FROM {table_name}
            WHERE name = $1::text
              AND owner = $2::text
              AND status IN ('queued', 'running')
              AND due_at <= $3::timestamptz
            ORDER BY id
            FOR UPDATE;
        """,
        "catch_up_owner": _CATCH_UP.replace(
            "{returning}", f"RETURNING {_ENDED}"
        ).replace(
            "{where}",
            """id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE name = $4::text
                  AND owner = $6::text
                  AND status IN ('queued', 'running')
                  AND due_at <= $1::timestamptz
                FOR UPDATE SKIP LOCKED
            ))""",
        ),
        "scan": """
            SELECT count(*), min(seq) FROM (
                SELECT seq FROM {table_name}
                WHERE name = $1::text AND owner = $2::text AND seq < $3::bigint
                ORDER BY seq DESC
                LIMIT $4::bigint
            ) AS scanned;
        """,
        "list": """
            SELECT seq, {record} FROM {table_name}
            WHERE name = $1::text
              AND owner = $2::text
              AND seq < $3::bigint
              AND seq >= $7::bigint
              AND (expires_at IS NULL OR expires_at > $4::timestamptz)
              AND ($5::text IS NULL OR status = $5::text)
            ORDER BY seq DESC
            LIMIT $6::bigint;
        """,
        "reap": _CATCH_UP.replace("{returning}", f"RETURNING {_ENDED}").replace(
            "{where}",
            """id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE status IN ('queued', 'running')
                  AND due_at <= $1::timestamptz
                LIMIT $4::bigint
                FOR UPDATE SKIP LOCKED
            ))""",
        ),
        "trim": """
            DELETE FROM {table_name} WHERE id = ANY(ARRAY(
                SELECT id FROM {table_name}
                WHERE id = ANY(ARRAY(
                    SELECT id FROM {table_name}
                    WHERE name = $1::text
                      AND (status = 'succeeded') = $2::boolean
                      AND finished_at IS NOT NULL
                      AND {capped}
                      AND (expires_at IS NULL OR expires_at > $3::timestamptz)
                    ORDER BY finished_at DESC, seq DESC
                    OFFSET $4::bigint
                ))
                FOR UPDATE SKIP LOCKED
            ));
        """,
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
                max_run_us, max_kept_succeeded, max_kept_failed, runner,
                started_at_submit, created_at, started_at, due_at, run_by
            )
            VALUES (
                $1::text, $2::text, $3::text, $4::text, 1, $5::text,
                $6::text, $7::bigint, $13::bigint, $15::bigint, $16::bigint,
                $8::text, $9::boolean, $10::timestamptz, $11::timestamptz,
                $12::timestamptz, $14::timestamptz
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING {record};
        """,
        "claim": """
            UPDATE {table_name} SET
                status = 'running',
                runner = $3::text,
                started_at = $1::timestamptz,
                run_by = $1::timestamptz
                    + max_run_us * INTERVAL '1 microsecond',
                due_at = LEAST(
                    $4::timestamptz,
                    $1::timestamptz + max_run_us * INTERVAL '1 microsecond'
                ),
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
                due_at = LEAST($2::timestamptz, run_by),
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
            WHERE id = $1::text
            RETURNING name, status = 'succeeded', CASE
                WHEN status = 'succeeded' THEN max_kept_succeeded
                ELSE max_kept_failed
            END;
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
            RETURNING {record}, """
            + _ENDED
            + """;
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
            key: template.format(
                table_name=table_name, record=_RECORD, capped=_CAPPED
            )
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

    async def submit(  # noqa: PLR0913
        self,
        *,
        name: str,
        job_id: str,
        owner: str | None,
        payload: str,
        context: Mapping[str, str],
        max_queued: int | None,
        max_wait: timedelta | None,
        max_run: timedelta | None,
        keep: timedelta | None,
        max_kept_succeeded: int | None,
        max_kept_failed: int | None,
        start_as: str | None,
        lease: timedelta,
    ) -> Submitted:
        """Store a job, or refuse it."""
        async with self._transaction(
            ids=[job_id], name=None if max_queued is None else name
        ) as (conn, began):
            now = began
            while True:
                ended: builtins.list[tuple[Any, ...]] = []
                stored = await self._find(conn, now, job_id, ended)
                if stored is not None:
                    await self._trim(conn, now, ended)
                    return _retried(stored, name, owner, start_as)
                ended += await self._end(conn, "catch_up_queue", now, name)
                await self._trim(conn, now, ended)
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
                run_by = None
                if runner is None:
                    due_at = None if max_wait is None else now + _span(max_wait)
                else:
                    due_at = now + _span(lease)
                    if max_run is not None:
                        run_by = now + _span(max_run)
                        due_at = min(due_at, run_by)
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
                    None if max_run is None else _us(max_run),
                    run_by,
                    max_kept_succeeded,
                    max_kept_failed,
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
    ) -> builtins.list[Claimed]:
        """Claim queued jobs up to each name's slots, oldest first per name."""
        names = [name for name, free in slots.items() if free > 0]
        if not names:
            return []
        claimed: builtins.list[Claimed] = []
        ended: builtins.list[tuple[Any, ...]] = []
        async with self._transaction() as (conn, now):
            for name in names:
                ended += await self._end(conn, "catch_up_queue", now, name)
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
            await self._trim(conn, now, ended)
        return claimed

    async def beat(
        self,
        *,
        runner: str,
        beats: Sequence[Beat],
        lease: timedelta,
    ) -> builtins.list[BeatReply]:
        """Extend each held job's lease, write its progress, read its flag."""
        if not beats:
            return []
        replies: builtins.list[BeatReply] = []
        ended: builtins.list[tuple[Any, ...]] = []
        async with self._transaction(ids=[b.job_id for b in beats]) as (
            conn,
            now,
        ):
            for beat in beats:
                row, caught = await self._look_up(conn, now, beat.job_id, ended)
                if (
                    row is None
                    or row.record.name != beat.name
                    or row.record.status is not JobStatus.RUNNING
                    or row.runner != runner
                ):
                    replies.append(
                        BeatReply(
                            beat.name,
                            beat.job_id,
                            held=False,
                            cancel_requested=False,
                            error=_store_error(row, beat.name, caught),
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
            await self._trim(conn, now, ended)
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
    ) -> Settled:
        """End a running job, and start its retention."""
        async with self._transaction(ids=[job_id]) as (conn, now):
            ended: builtins.list[tuple[Any, ...]] = []
            row, caught = await self._look_up(conn, now, job_id, ended)
            await self._trim(conn, now, ended)
            if row is None or row.record.name != name or row.runner != runner:
                return Settled(
                    settled=False, error=_store_error(row, name, caught)
                )
            if row.record.status is not JobStatus.RUNNING:
                return Settled(settled=row.record.status is status, error=None)
            done, total = (
                (row.record.done, row.record.total)
                if progress is None
                else (progress.done, progress.total)
            )
            settled = await conn.fetchrow(
                self._sql["settle"],
                job_id,
                status.value,
                error,
                result,
                done,
                total,
                now,
            )
            await self._trim(conn, now, [tuple(settled)])
        return Settled(settled=True, error=None)

    async def get(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Return the job when `owner` owns it, else `None`."""
        async with self._transaction(ids=[job_id]) as (conn, now):
            ended: builtins.list[tuple[Any, ...]] = []
            row = await self._owned(conn, now, name, job_id, owner, ended)
            await self._trim(conn, now, ended)
            if row is not None and ended:
                row = await self._owned(conn, now, name, job_id, owner, [])
        return None if row is None else row.record

    async def cancel(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Ask the job to stop when `owner` owns it, and return it."""
        async with self._transaction(ids=[job_id]) as (conn, now):
            ended: builtins.list[tuple[Any, ...]] = []
            row = await self._owned(conn, now, name, job_id, owner, ended)
            await self._trim(conn, now, ended)
            if row is not None and ended:
                row = await self._owned(conn, now, name, job_id, owner, [])
            if row is None:
                return None
            record = row.record
            if record.status is JobStatus.QUEUED:
                cancelled = await conn.fetchrow(
                    self._sql["cancel_queued"], job_id, now
                )
                await self._trim(conn, now, [tuple(cancelled)[-3:]])
                return _record(cancelled)
            if (
                record.status is JobStatus.RUNNING
                and not record.cancel_requested
            ):
                flagged = await conn.fetchrow(
                    self._sql["cancel_running"], job_id
                )
                return _record(flagged)
        return record

    async def list(
        self,
        *,
        name: str,
        owner: str,
        status: JobStatus | None,
        limit: int,
        after: str | None,
    ) -> Listed:
        """Return the jobs of `name` that `owner` owns, newest submitted first.

        The owner's jobs past a lease, `max_wait` or `max_run` end first, in
        the same transaction. It locks them in id order, as `beat` does,
        then reads the clock again, so each end follows the move it waited
        for. A job that came due since, and that another move holds, is
        left to that move.
        """
        below = FIRST if after is None else read_cursor(after)
        async with self._transaction() as (conn, began):
            await conn.execute(self._sql["lock_owner"], name, owner, began)
            now = await conn.fetchval(self._sql["now"])
            ended = await conn.fetch(
                self._sql["catch_up_owner"],
                now,
                JOB_LOST,
                JOB_NOT_STARTED,
                name,
                JOB_TIMED_OUT,
                owner,
            )
            await self._trim(conn, now, [tuple(row) for row in ended])
            bound = scan_limit(limit)
            scanned, floor = await conn.fetchrow(
                self._sql["scan"], name, owner, below, bound
            )
            if not scanned:
                return Listed([], None)
            rows = await conn.fetch(
                self._sql["list"],
                name,
                owner,
                below,
                now,
                None if status is None else status.value,
                limit + 1,
                floor,
            )
        return page_of(
            [(row[0], _record(tuple(row)[1:])) for row in rows],
            limit,
            floor if scanned == bound else None,
        )

    async def reap(self, *, limit: int) -> int:
        """End jobs past a lease, `max_wait` or `max_run`, of any name."""
        async with self._transaction() as (conn, now):
            ended = await self._end(conn, "reap", now, limit)
            await self._trim(conn, now, ended)
        return len(ended)

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

        The transaction first takes the row lock of each job of `ids` in id
        order, then the advisory lock of `name`, when set, and only then
        reads the clock. A cap applied at the end of a call takes the
        advisory lock of a name after the row locks too. A move that waited
        for another one therefore never writes a time before it.
        `READ COMMITTED` gives each statement a fresh snapshot, so a read
        after a lock sees what the holder wrote.

        Before its first transaction the adapter creates the table and its
        indexes, unless `auto_migrate` is off.
        """
        async with self._provider.client.acquire() as conn:
            if not self._migrated:
                await self._migrate(conn)
            async with conn.transaction(isolation="read_committed"):
                if ids:
                    await conn.execute(self._sql["lock"], list(ids))
                if name is not None:
                    await conn.execute(self._sql["lock_name"], name)
                yield conn, await conn.fetchval(self._sql["now"])

    async def _migrate(self, conn: Connection) -> None:
        """Create the table and indexes, guarded so replicas do not race."""
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", self._table_name
            )
            await conn.execute(
                self._SQL_CREATE.format(
                    table_name=self._table_name, capped=_CAPPED
                )
            )
        self._migrated = True

    async def _end(
        self,
        conn: Connection,
        key: str,
        now: datetime,
        arg: Any,  # noqa: ANN401
    ) -> builtins.list[tuple[Any, ...]]:
        """Run a catch-up write at `now`, and return the jobs it ended.

        Each is the job's name, whether it succeeded, and the cap of its
        kind, for `_trim`.
        """
        rows = await conn.fetch(
            self._sql[key],
            now,
            JOB_LOST,
            JOB_NOT_STARTED,
            arg,
            JOB_TIMED_OUT,
        )
        return [tuple(row) for row in rows]

    async def _trim(
        self,
        conn: Connection,
        now: datetime,
        ended: Iterable[tuple[Any, ...]],
    ) -> None:
        """Forget the oldest ended jobs past the cap of each job that ended.

        Each item of `ended` is a job's name, whether it succeeded, and the
        cap of its kind, `None` for no cap. Names are locked in order, with
        the advisory lock `max_queued` takes, so two ends of one name never
        both see room under its cap. The delete skips a job another call
        holds at that moment, so it never waits on a row while it holds a
        name.
        """
        for name, succeeded, cap in sorted(
            {tuple(row) for row in ended if row[2] is not None}
        ):
            await conn.execute(self._sql["lock_name"], name)
            await conn.execute(self._sql["trim"], name, succeeded, now, cap)

    async def _find(
        self,
        conn: Connection,
        now: datetime,
        job_id: str,
        ended: builtins.list[tuple[Any, ...]],
    ) -> _Stored | None:
        """Return the job as it is now, or `None` when it is gone."""
        stored, _ = await self._look_up(conn, now, job_id, ended)
        return stored

    async def _look_up(
        self,
        conn: Connection,
        now: datetime,
        job_id: str,
        ended: builtins.list[tuple[Any, ...]],
    ) -> tuple[_Stored | None, tuple[str, str] | None]:
        """Return the job as it is now, and what a catch-up ended now.

        That is the name and error of the job when this call ended it, so
        they stay known when the job is gone at once, with `keep` at zero.
        A job the catch-up ended joins `ended`, for the caller's `_trim`.
        """
        caught_up = await conn.fetchrow(
            self._sql["catch_up"],
            now,
            JOB_LOST,
            JOB_NOT_STARTED,
            job_id,
            JOB_TIMED_OUT,
        )
        await conn.execute(self._sql["forget"], job_id, now)
        row = await conn.fetchrow(self._sql["find"], job_id)
        caught = None
        if caught_up is not None:
            caught = (caught_up[1], caught_up[0])
            ended.append(tuple(caught_up)[1:])
        if row is None:
            return None, caught
        return _Stored(_record(row), row[12], row[13], row[14]), caught

    async def _owned(
        self,
        conn: Connection,
        now: datetime,
        name: str,
        job_id: str,
        owner: str | None,
        ended: builtins.list[tuple[Any, ...]],
    ) -> _Stored | None:
        """Return the job when it has this name and `owner` owns it."""
        row = await self._find(conn, now, job_id, ended)
        if row is None or row.record.name != name or row.owner != owner:
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


def _store_error(
    row: _Stored | None, name: str, ended: tuple[str, str] | None
) -> str | None:
    """Return why the store ended a job `failed`, if it did.

    `ended` is the name and error of the job a catch-up ended in this call.
    """
    if ended is not None:
        return ended[1] if ended[0] == name else None
    if row is None or row.record.name != name or row.runner is not None:
        return None
    return row.record.error if row.record.status is JobStatus.FAILED else None


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
