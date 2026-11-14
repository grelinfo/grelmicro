"""In-memory Job Adapter.

For tests and single-process apps. Jobs live in the process and are lost on
restart. Each method runs without an `await`, so on one event loop it is one
atomic step.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from heapq import heappop, heappush
from typing import TYPE_CHECKING, ClassVar, Self

from grelmicro.clock._seam import monotonic
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
    from collections.abc import Mapping, Sequence
    from datetime import timedelta
    from types import TracebackType

    from grelmicro.types import BackendScope


@dataclass(slots=True)
class _Row:
    """A stored job."""

    id: str
    seq: int
    name: str
    owner: str | None
    payload: str
    context: Mapping[str, str]
    keep: timedelta | None
    max_run: timedelta | None
    max_kept_succeeded: int | None
    max_kept_failed: int | None
    created_at: datetime
    status: JobStatus = JobStatus.QUEUED
    version: int = 1
    done: int = 0
    total: int | None = None
    result: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    runner: str | None = None
    started_at_submit: bool = False
    lease_until: float = 0.0
    start_by: float | None = None
    run_by: float | None = None
    expires_at: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def record(self) -> JobRecord:
        """Return what a reader may see of this row."""
        return JobRecord(
            id=self.id,
            name=self.name,
            status=self.status,
            version=self.version,
            done=self.done,
            total=self.total,
            result=self.result,
            error=self.error,
            cancel_requested=self.cancel_requested,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


def _after(duration: timedelta) -> float:
    """Return the time `duration` from now."""
    return monotonic() + duration.total_seconds()


def _start(row: _Row, runner: str, lease: timedelta) -> None:
    """Move `row` to `running`, held by `runner`."""
    row.status = JobStatus.RUNNING
    row.runner = runner
    row.lease_until = _after(lease)
    row.run_by = _deadline(row.max_run)
    row.started_at = datetime.now(UTC)


def _write_progress(row: _Row, progress: ProgressUpdate | None) -> bool:
    """Write `progress` to `row`, and return whether it changed anything."""
    if progress is None or (row.done, row.total) == (
        progress.done,
        progress.total,
    ):
        return False
    row.done, row.total = progress.done, progress.total
    return True


def _store_error(row: _Row | None, name: str) -> str | None:
    """Return why the store ended the job `row` `failed`, if it did."""
    if row is None or row.name != name or row.runner is not None:
        return None
    return row.error if row.status is JobStatus.FAILED else None


def _seq(row: _Row) -> int:
    """Return the submission order of `row`."""
    return row.seq


def _deadline(after: timedelta | None) -> float | None:
    """Return the time `after` from now, or `None` for no limit."""
    return None if after is None else _after(after)


class MemoryJobAdapter:
    """In-memory job storage backend."""

    scope: ClassVar[BackendScope] = "process"
    """State lives in this process and is not shared beyond it."""

    def __init__(self) -> None:
        """Initialize an empty in-memory backend."""
        self._rows: dict[str, _Row] = {}
        self._queued: defaultdict[str, deque[_Row]] = defaultdict(deque)
        self._open: dict[str, _Row] = {}
        self._expiring: dict[str, _Row] = {}
        self._owned: defaultdict[tuple[str, str], builtins.list[_Row]] = (
            defaultdict(list)
        )
        """The jobs of each name and owner, in submission order."""
        self._kept: defaultdict[tuple[str, bool], dict[str, _Row]] = (
            defaultdict(dict)
        )
        """The ended jobs of each name, by success or not, in end order."""
        self._kept_ends: defaultdict[
            tuple[str, bool], builtins.list[tuple[float, int, _Row]]
        ] = defaultdict(list)
        """When each kept job of `_kept` expires, soonest first, as a heap."""
        self._seq = 0

    async def __aenter__(self) -> Self:
        """Open the backend."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend."""

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
        stored = self._find(name, job_id)
        if stored is not None and stored.owner == owner:
            started = (
                stored.started_at_submit
                and stored.status is JobStatus.RUNNING
                and stored.runner == start_as
            )
            return Submitted(stored.record(), started=started)
        if job_id in self._rows:
            return Submitted(None, started=False)
        queued = self._queued[name]
        if max_queued is not None or start_as is not None:
            for waiting in list(queued):
                self._catch_up(waiting)
        runner = None if queued else start_as
        if (
            runner is None
            and max_queued is not None
            and len(queued) >= max_queued
        ):
            return Submitted(None, started=False)
        self._seq += 1
        row = _Row(
            id=job_id,
            seq=self._seq,
            name=name,
            owner=owner,
            payload=payload,
            context=dict(context),
            keep=keep,
            max_run=max_run,
            max_kept_succeeded=max_kept_succeeded,
            max_kept_failed=max_kept_failed,
            created_at=datetime.now(UTC),
            start_by=_deadline(max_wait),
        )
        self._rows[job_id] = row
        self._open[job_id] = row
        if owner is not None:
            self._owned[name, owner].append(row)
        if runner is None:
            queued.append(row)
        else:
            _start(row, runner, lease)
            row.started_at_submit = True
        return Submitted(row.record(), started=runner is not None)

    async def claim(
        self,
        *,
        slots: Mapping[str, int],
        runner: str,
        lease: timedelta,
    ) -> builtins.list[Claimed]:
        """Claim queued jobs up to each name's slots, oldest first per name."""
        claimed: builtins.list[Claimed] = []
        for name, free in slots.items():
            queued = self._queued[name]
            taken = 0
            while queued and taken < free:
                row = queued[0]
                if self._catch_up(row):
                    continue
                queued.popleft()
                _start(row, runner, lease)
                row.version += 1
                claimed.append(Claimed(row.record(), row.payload, row.context))
                taken += 1
        return claimed

    async def get(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Return the job when `owner` owns it, else `None`."""
        row = self._find(name, job_id)
        if row is None or row.owner != owner:
            return None
        return row.record()

    async def beat(
        self,
        *,
        runner: str,
        beats: Sequence[Beat],
        lease: timedelta,
    ) -> builtins.list[BeatReply]:
        """Extend each held job's lease, write its progress, read its flag."""
        replies: builtins.list[BeatReply] = []
        for beat in beats:
            stored = self._rows.get(beat.job_id)
            row = self._held(beat.name, beat.job_id, runner)
            if row is None:
                replies.append(
                    BeatReply(
                        beat.name,
                        beat.job_id,
                        held=False,
                        cancel_requested=False,
                        error=_store_error(stored, beat.name),
                    )
                )
                continue
            row.lease_until = _after(lease)
            if _write_progress(row, beat.progress):
                row.version += 1
            replies.append(
                BeatReply(
                    beat.name,
                    beat.job_id,
                    held=True,
                    cancel_requested=row.cancel_requested,
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
    ) -> Settled:
        """End a running job, and start its retention."""
        stored = self._rows.get(job_id)
        row = self._find(name, job_id)
        if row is None or row.runner != runner:
            return Settled(settled=False, error=_store_error(stored, name))
        if row.status is not JobStatus.RUNNING:
            return Settled(settled=row.status is status, error=None)
        _write_progress(row, progress)
        row.result = result
        self._end(row, status, error)
        return Settled(settled=True, error=None)

    async def cancel(
        self, *, name: str, job_id: str, owner: str | None
    ) -> JobRecord | None:
        """Ask the job to stop when `owner` owns it, and return it."""
        row = self._find(name, job_id)
        if row is None or row.owner != owner:
            return None
        if row.status is JobStatus.QUEUED:
            self._queued[name].remove(row)
            row.cancel_requested = True
            self._end(row, JobStatus.CANCELLED)
        elif row.status is JobStatus.RUNNING and not row.cancel_requested:
            row.cancel_requested = True
            row.version += 1
        return row.record()

    async def list(
        self,
        *,
        name: str,
        owner: str,
        status: JobStatus | None,
        limit: int,
        after: str | None,
    ) -> Listed:
        """Return the jobs of `name` that `owner` owns, newest submitted first."""
        below = FIRST if after is None else read_cursor(after)
        owned = self._owned.get((name, owner), [])
        end = bisect_left(owned, below, key=_seq)
        bound = scan_limit(limit)
        scanned = owned[max(0, end - bound) : end]
        found: builtins.list[tuple[int, JobRecord]] = []
        for row in reversed(scanned):
            if self._find(name, row.id) is None:
                continue
            if status is None or row.status == status:
                found.append((row.seq, row.record()))
                if len(found) > limit:
                    return page_of(found, limit, None)
        stopped = scanned[0].seq if len(scanned) == bound else None
        return page_of(found, limit, stopped)

    async def reap(self, *, limit: int) -> int:
        """End jobs past a lease, `max_wait` or `max_run`, of any name."""
        reaped = 0
        for row in list(self._open.values()):
            if reaped == limit:
                break
            reaped += self._catch_up(row)
        return reaped

    async def purge(self, *, limit: int) -> int:
        """Forget ended jobs past their retention, whatever their name."""
        purged = 0
        now = monotonic()
        for row in list(self._expiring.values()):
            if purged == limit:
                break
            if row.expires_at is not None and row.expires_at <= now:
                self._forget(row)
                purged += 1
        return purged

    def _held(self, name: str, job_id: str, runner: str) -> _Row | None:
        """Return the running job `job_id` when `runner` holds it."""
        row = self._find(name, job_id)
        if (
            row is None
            or row.runner != runner
            or row.status is not JobStatus.RUNNING
        ):
            return None
        return row

    def _find(self, name: str, job_id: str) -> _Row | None:
        """Return the job as it is now, or `None` when it is gone."""
        row = self._rows.get(job_id)
        if row is None:
            return None
        self._catch_up(row)
        if self._rows.get(job_id) is not row:
            return None
        if row.expires_at is not None and row.expires_at <= monotonic():
            self._forget(row)
            return None
        return row if row.name == name else None

    def _catch_up(self, row: _Row) -> bool:
        """Make the move `row` is overdue for, and return whether it did."""
        now = monotonic()
        if row.status is JobStatus.RUNNING:
            ends_at = row.lease_until
            if row.run_by is not None:
                ends_at = min(ends_at, row.run_by)
            if ends_at > now:
                return False
            row.runner = None
            error = JOB_TIMED_OUT if row.run_by == ends_at else JOB_LOST
            self._end(row, JobStatus.FAILED, error)
            return True
        if (
            row.status is JobStatus.QUEUED
            and row.start_by is not None
            and row.start_by <= now
        ):
            self._queued[row.name].remove(row)
            self._end(row, JobStatus.FAILED, JOB_NOT_STARTED)
            return True
        return False

    def _end(
        self, row: _Row, status: EndStatus, error: str | None = None
    ) -> None:
        """Move `row` to an end state, drop its arguments, start its retention.

        Past the cap of its kind, the oldest ended jobs of its name go.
        """
        row.status = status
        row.error = error
        row.payload = ""
        row.context = {}
        row.version += 1
        row.finished_at = datetime.now(UTC)
        row.expires_at = _deadline(row.keep)
        del self._open[row.id]
        if row.expires_at is not None:
            self._expiring[row.id] = row
        succeeded = status is JobStatus.SUCCEEDED
        cap = row.max_kept_succeeded if succeeded else row.max_kept_failed
        if cap is not None:
            index = (row.name, succeeded)
            kept, ends = self._kept[index], self._kept_ends[index]
            now = monotonic()
            while ends and ends[0][0] <= now:
                _, _, expired = heappop(ends)
                if kept.get(expired.id) is expired:
                    self._forget(expired)
            kept, ends = self._kept[index], self._kept_ends[index]
            if row.keep is None or row.keep:
                kept[row.id] = row
                if row.expires_at is not None:
                    heappush(ends, (row.expires_at, row.seq, row))
            while len(kept) > cap:
                self._forget(next(iter(kept.values())))
            if not kept:
                self._kept.pop(index, None)
                self._kept_ends.pop(index, None)

    def _forget(self, row: _Row) -> None:
        """Remove an ended job for good."""
        del self._rows[row.id]
        self._expiring.pop(row.id, None)
        index = (row.name, row.status is JobStatus.SUCCEEDED)
        kept = self._kept.get(index)
        if kept is not None and kept.pop(row.id, None) is not None and not kept:
            del self._kept[index]
            self._kept_ends.pop(index, None)
        if row.owner is not None:
            owned = self._owned[row.name, row.owner]
            del owned[bisect_left(owned, row.seq, key=_seq)]
            if not owned:
                del self._owned[row.name, row.owner]
