"""Job Backend Protocol.

This module defines a `typing.Protocol`. Methods end with `...` because
the protocol describes a structural contract, not an implementation.
Concrete backends (`MemoryJobAdapter`) provide the bodies.

A job only moves forward, and each move below is one atomic step in the store:

| Move | Who | Guard, in the same step |
| --- | --- | --- |
| none to `queued` | `submit` | under `max_queued` |
| none to `running` | `submit`, with `start_as` | nothing of the name queued |
| `queued` to `running` | `claim` | still `queued` |
| `running`, lease extended, progress written | `beat` | runner holds it |
| `running` to an end state | `settle` | runner holds it |
| `queued` to `cancelled` | `cancel` | owner matches |
| `running`, cancel flag set | `cancel` | owner matches |
| `running` to `failed`, lease lapsed | `reap`, or a read | lease lapsed |
| `running` to `failed`, past `max_run` | `reap`, or a read | past `max_run` |
| `queued` to `failed`, past `max_wait` | `reap`, or a read | past `max_wait` |
| an end state to gone | `purge`, or the store | past `keep` |

A job past both its lease and its `max_run` ends with the error of the one
that passed first. The store's own clock decides every lease and deadline. A
job that reaches an end state no longer holds its payload or context.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import (
    TYPE_CHECKING,
    Annotated,
    ClassVar,
    Literal,
    NamedTuple,
    Protocol,
    Self,
    runtime_checkable,
)

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime, timedelta
    from types import TracebackType

    from grelmicro.types import BackendScope

JOB_LOST = "Job lost"
"""The error of a job whose lease lapsed."""

JOB_NOT_STARTED = "Job not started in time"
"""The error of a queued job past its `max_wait`."""

JOB_TIMED_OUT = "Job not finished in time"
"""The error of a running job past its `max_run`."""


class JobStatus(StrEnum):
    """Status of a job.

    - ``QUEUED``: waiting for a free slot.
    - ``RUNNING``: the body is running.
    - ``SUCCEEDED``: the body returned.
    - ``FAILED``: the body raised. `JobInfo.error` says why.
    - ``CANCELLED``: cancelled before or while it ran.

    Read more in the [Status](../task.md#status) docs.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


type EndStatus = Literal[
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED
]
"""A status a job never leaves."""


@dataclass(frozen=True, slots=True)
class JobRecord:
    """What every read returns. Never the owner, payload, runner or lease."""

    id: Annotated[str, Doc("The job's id.")]
    name: Annotated[str, Doc("The job's name.")]
    status: Annotated[JobStatus, Doc("Where the job is in its run.")]
    version: Annotated[
        int, Doc("Raised by one by each change the other fields show.")
    ]
    done: Annotated[int, Doc("Items the job reported done.")]
    total: Annotated[int | None, Doc("Items the job expects, if known.")]
    result: Annotated[str | None, Doc("The result, as opaque JSON.")]
    error: Annotated[str | None, Doc("Why the job failed.")]
    cancel_requested: Annotated[bool, Doc("Whether a cancel was requested.")]
    created_at: Annotated[datetime, Doc("When the job was submitted.")]
    started_at: Annotated[datetime | None, Doc("When the job started.")]
    finished_at: Annotated[datetime | None, Doc("When the job ended.")]


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """A running job's counts, as absolute values."""

    done: Annotated[int, Doc("Items done so far.")]
    total: Annotated[int | None, Doc("Items expected, if known.")]


@dataclass(frozen=True, slots=True)
class Claimed:
    """A job a runner now holds, and what it needs to run the body."""

    record: Annotated[JobRecord, Doc("The job, now `running`.")]
    payload: Annotated[str, Doc("The arguments, as opaque JSON.")]
    context: Annotated[Mapping[str, str], Doc("The text stored at submit.")]


@dataclass(frozen=True, slots=True)
class Beat:
    """One held job in a `beat`, with its progress when it changed."""

    name: Annotated[str, Doc("The job's name.")]
    job_id: Annotated[str, Doc("The job's id.")]
    progress: Annotated[
        ProgressUpdate | None, Doc("The counts, when they changed.")
    ] = None


@dataclass(frozen=True, slots=True)
class BeatReply:
    """What a `beat` found for one job."""

    name: Annotated[str, Doc("The job's name.")]
    job_id: Annotated[str, Doc("The job's id.")]
    held: Annotated[
        bool, Doc("Whether the runner still holds the job. If not, stop.")
    ]
    cancel_requested: Annotated[bool, Doc("Whether a cancel was requested.")]
    error: Annotated[
        str | None,
        Doc("Why the store ended the job, when it no longer holds it."),
    ] = None


class Submitted(NamedTuple):
    """What a `submit` did."""

    record: Annotated[JobRecord | None, Doc("The job, or `None` if refused.")]
    started: Annotated[bool, Doc("Whether the caller must run the body now.")]


class Settled(NamedTuple):
    """What a `settle` did."""

    settled: Annotated[bool, Doc("Whether the store took the end.")]
    error: Annotated[
        str | None,
        Doc("Why the store ended the job itself, when it refused."),
    ]


@runtime_checkable
class JobBackend(Protocol):
    """Protocol for job storage backends."""

    scope: ClassVar[BackendScope]
    """How far the backend's state is shared."""

    async def __aenter__(self) -> Self:
        """Open the backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend."""
        ...

    async def submit(  # noqa: PLR0913
        self,
        *,
        name: Annotated[str, Doc("The job's name.")],
        job_id: Annotated[
            str,
            Doc("The id, chosen by the caller, unique across every name."),
        ],
        owner: Annotated[str | None, Doc("Who the job belongs to.")],
        payload: Annotated[str, Doc("The arguments, as opaque JSON.")],
        context: Annotated[
            Mapping[str, str],
            Doc("Text the runner needs, such as the trace context."),
        ],
        max_queued: Annotated[
            int | None,
            Doc("Queued jobs of the name the store accepts. `None`: no limit."),
        ],
        max_wait: Annotated[
            timedelta | None,
            Doc("How long the job may stay queued. `None`: no limit."),
        ],
        max_run: Annotated[
            timedelta | None,
            Doc("How long the job may run once it starts. `None`: no limit."),
        ],
        keep: Annotated[
            timedelta | None,
            Doc("How long the ended job stays readable. `None`: no limit."),
        ],
        start_as: Annotated[
            str | None,
            Doc("The runner, when this replica has a free slot."),
        ],
        lease: Annotated[timedelta, Doc("The lease, with `start_as`.")],
    ) -> Submitted:
        """Store a job, or refuse it.

        With `start_as`, and nothing of the name queued, the job starts
        `running`, held by that runner, and `started` is `True`. Otherwise it
        is `queued`, unless `max_queued` jobs of the name already are: then
        `record` is `None`. `max_queued=0` therefore means run now or refuse.
        The job keeps its `max_run`, counted from when it starts, on
        whichever runner starts it.

        A retried `submit` with the same `job_id` returns the stored job and
        stores nothing. `started` is `True` only when the first call started
        the job as `start_as`, so a job claimed later never runs twice. Retry
        only while the job is kept: past its retention, a retry stores a new
        job. A `job_id` taken by another name or owner is refused, and that
        job does not change.
        """
        ...

    async def claim(
        self,
        *,
        slots: Annotated[
            Mapping[str, int],
            Doc("How many jobs to claim at most, for each name to claim."),
        ],
        runner: Annotated[str, Doc("The token of the claiming runner.")],
        lease: Annotated[timedelta, Doc("The lease on each claimed job.")],
    ) -> list[Claimed]:
        """Claim queued jobs up to each name's slots, oldest first per name.

        Two concurrent claims never return the same job, and a job is claimed
        at most once.
        """
        ...

    async def beat(
        self,
        *,
        runner: Annotated[str, Doc("The token of the runner.")],
        beats: Annotated[Sequence[Beat], Doc("One entry per held job.")],
        lease: Annotated[timedelta, Doc("The new lease on each held job.")],
    ) -> list[BeatReply]:
        """Extend each held job's lease, write its progress, read its flag.

        A job this runner no longer holds comes back with `held=False`, and
        nothing of its entry is written. When the store ended that job
        `failed` itself, such as past its lease or `max_run`, `error` says
        why.
        """
        ...

    async def settle(
        self,
        *,
        name: Annotated[str, Doc("The job's name.")],
        job_id: Annotated[str, Doc("The job's id.")],
        runner: Annotated[str, Doc("The token of the runner.")],
        status: Annotated[EndStatus, Doc("The end state.")],
        error: Annotated[str | None, Doc("Why the job failed.")],
        result: Annotated[str | None, Doc("The result, as opaque JSON.")],
        progress: Annotated[ProgressUpdate | None, Doc("The last counts.")],
    ) -> Settled:
        """End a running job, and start its retention.

        Refuses, and writes nothing, when `runner` no longer holds the job.
        When the store ended the job `failed` itself, such as past its lease
        or `max_run`, `error` says why. It is `None` for a job that is gone
        or that another runner ended. Retrying with the same arguments
        after a lost reply is taken and writes nothing more, while the job
        is still kept. With `keep` at zero the job is already gone, so the
        retry is refused.
        """
        ...

    async def get(
        self,
        *,
        name: Annotated[str, Doc("The job's name.")],
        job_id: Annotated[str, Doc("The job's id.")],
        owner: Annotated[str | None, Doc("Who is reading.")],
    ) -> JobRecord | None:
        """Return the job when `owner` owns it, else `None`.

        A read never shows a job as `running` past its lease or its
        `max_run`, or `queued` past its `max_wait`: it makes that move
        first. A job past its retention reads as `None`.
        """
        ...

    async def cancel(
        self,
        *,
        name: Annotated[str, Doc("The job's name.")],
        job_id: Annotated[str, Doc("The job's id.")],
        owner: Annotated[str | None, Doc("Who is cancelling.")],
    ) -> JobRecord | None:
        """Ask the job to stop when `owner` owns it, and return it.

        A `queued` job ends `cancelled`. A `running` job gets its cancel
        flag. A job in an end state does not change. Same not-found rules as
        `get`.
        """
        ...

    async def reap(
        self, *, limit: Annotated[int, Doc("How many jobs to end at most.")]
    ) -> int:
        """End jobs past a lease, `max_wait` or `max_run`, of any name."""
        ...

    async def purge(
        self, *, limit: Annotated[int, Doc("How many jobs to forget at most.")]
    ) -> int:
        """Forget ended jobs past their retention, whatever their name.

        Returns how many it forgot. A store that expires jobs on its own
        returns `0`.
        """
        ...
