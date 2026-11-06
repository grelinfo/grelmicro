"""The one store loop a replica runs for all its jobs.

Jobs on one store share one loop, so the store sees the same calls for one job
as for many: one `beat` and at most one `claim` each second, and one sweep now
and then. The first job to join opens the store, and the last to leave closes it.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol
from weakref import WeakKeyDictionary

from grelmicro.clock._seam import sleep

if TYPE_CHECKING:
    from grelmicro.task._protocol import Beat, BeatReply, Claimed, JobBackend

__all__ = ["LEASE", "JobStoreLoop", "store_loop"]

LEASE = timedelta(seconds=30)
"""How long a job stays held without a beat."""

_BEAT = timedelta(seconds=1)
"""How often a replica extends leases, writes counts and reads cancels."""

_SWEEP_EVERY_BEATS = 10
"""How many beats pass between two sweeps of the store."""

_SWEEP_LIMIT = 100
"""How many jobs one sweep ends or forgets at most."""


class _Member(Protocol):
    """What the loop needs from a job on its store."""

    @property
    def name(self) -> str:
        """The job's name, unique on the store."""
        ...  # pragma: no cover

    def _reserve_slots(self) -> int:
        """Reserve and return every free slot, none when it takes no more."""
        ...  # pragma: no cover

    def _give_back_slots(self, slots: int) -> None:
        """Give back slots a claim did not use."""
        ...  # pragma: no cover

    def _accept(self, job: Claimed) -> None:
        """Start a claimed job in a reserved slot."""
        ...  # pragma: no cover

    def _beats(self) -> list[Beat]:
        """Return one entry per job this member runs."""
        ...  # pragma: no cover

    def _read_reply(self, reply: BeatReply) -> None:
        """Act on what a beat found for one of its jobs."""
        ...  # pragma: no cover


class JobStoreLoop:
    """Beats, claims and sweeps one store, for every job of a replica on it."""

    def __init__(self, backend: JobBackend) -> None:
        self._backend = backend
        self.runner = secrets.token_urlsafe(16)
        """The token this replica holds its jobs with."""
        self._members: dict[str, _Member] = {}
        self._wake = asyncio.Event()
        self._round = asyncio.Lock()
        self._joining = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        """Look for queued jobs now, instead of at the next beat."""
        self._wake.set()

    async def join(self, member: _Member) -> None:
        """Add `member`, opening the store for the first one.

        Raises:
            ValueError: If a job with the same name is already on the store.
        """
        async with self._joining:
            if member.name in self._members:
                msg = f"A job named {member.name!r} is already on this store"
                raise ValueError(msg)
            if self._task is None:
                await self._backend.__aenter__()
                self._task = asyncio.create_task(self._run())
            self._members[member.name] = member

    async def finish_round(self) -> None:
        """Wait for the store call in progress, and what it handed out."""
        async with self._round:
            pass

    async def leave(self, member: _Member) -> None:
        """Remove `member`, closing the store after the last one."""
        async with self._joining:
            async with self._round:
                del self._members[member.name]
            if self._members:
                return
            _loops.pop(self._backend, None)
            task = self._task
            assert task is not None  # noqa: S101
            self._task = None
            task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                await self._backend.__aexit__(None, None, None)

    async def _run(self) -> None:
        """Beat and claim until cancelled."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._beat_loop())
            group.create_task(self._claim_loop())

    async def _beat_loop(self) -> None:
        """Beat every second and sweep the store now and then."""
        beats = 0
        while True:
            await sleep(_BEAT.total_seconds())
            await self._beat()
            self._wake.set()
            beats += 1
            if beats % _SWEEP_EVERY_BEATS == 0:
                await self._backend.reap(limit=_SWEEP_LIMIT)
                await self._backend.purge(limit=_SWEEP_LIMIT)

    async def _beat(self) -> None:
        """Extend each lease, write new counts, and read each cancel flag."""
        async with self._round:
            beats = [
                beat
                for member in self._members.values()
                for beat in member._beats()
            ]
            if not beats:
                return
            replies = await self._backend.beat(
                runner=self.runner, beats=beats, lease=LEASE
            )
            for reply in replies:
                self._members[reply.name]._read_reply(reply)

    async def _claim_loop(self) -> None:
        """Run queued jobs, oldest first, when woken."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self._claim()

    async def _claim(self) -> None:
        """Claim queued jobs for every free slot, in one store call."""
        async with self._round:
            slots = {
                name: free
                for name, member in self._members.items()
                if (free := member._reserve_slots()) > 0
            }
            if not slots:
                return
            claimed = await self._backend.claim(
                slots=slots, runner=self.runner, lease=LEASE
            )
            for job in claimed:
                slots[job.record.name] -= 1
                self._members[job.record.name]._accept(job)
            for name, unused in slots.items():
                self._members[name]._give_back_slots(unused)


_loops: WeakKeyDictionary[JobBackend, JobStoreLoop] = WeakKeyDictionary()
"""The loop of each store this replica has open."""


def store_loop(backend: JobBackend) -> JobStoreLoop:
    """Return the loop of `backend`, a new one when it has none."""
    loop = _loops.get(backend)
    if loop is None:
        loop = _loops[backend] = JobStoreLoop(backend)
    return loop
