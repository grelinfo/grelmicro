"""The one store loop a replica runs for all its jobs.

Jobs on one store share one loop, so the store sees the same calls for one job
as for many: one `beat` and at most one `claim` each second, and one sweep now
and then. The loop opens and closes only a store the job built itself. Whoever
built any other store opens and closes it.

A store call that fails is tried again at the next beat. When no beat reached
the store for a whole lease, the store has given the jobs to others, so the loop
asks every body to stop.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Protocol
from weakref import WeakKeyDictionary

from grelmicro.clock._seam import monotonic, sleep

if TYPE_CHECKING:
    from grelmicro.task._protocol import Beat, BeatReply, Claimed, JobBackend

__all__ = ["BEAT", "LEASE", "JobStoreLoop", "store_loop"]

logger = getLogger("grelmicro.task")

LEASE = timedelta(seconds=30)
"""How long a job stays held without a beat."""

BEAT = timedelta(seconds=1)
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

    def _stop_runs(self) -> None:
        """Ask every job this member runs to stop."""
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

    def __init__(self, backend: JobBackend, *, opens: bool) -> None:
        self._backend = backend
        self._opens = opens
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
                if self._opens:
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
                if self._opens:
                    await self._backend.__aexit__(None, None, None)

    async def _run(self) -> None:
        """Beat and claim until cancelled."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._beat_loop())
            group.create_task(self._claim_loop())

    async def _beat_loop(self) -> None:
        """Beat every second and sweep the store now and then."""
        beats = 0
        held_at = monotonic()
        while True:
            await sleep(BEAT.total_seconds())
            beat_at = monotonic()
            error = await self._beat()
            held_at = (
                beat_at if error is None else self._missed_beat(held_at, error)
            )
            self._wake.set()
            beats += 1
            if beats % _SWEEP_EVERY_BEATS == 0:
                await self._sweep()

    def _missed_beat(self, held_at: float, error: Exception) -> float:
        """Stop every body once no beat reached the store for a lease.

        Returns when the jobs were last held, or now when they were let go.
        """
        if monotonic() - held_at < LEASE.total_seconds():
            logger.debug("Job beat failed, retrying", exc_info=error)
            return held_at
        logger.warning(
            "Job store unreachable for a whole lease, stopping its jobs",
            exc_info=error,
        )
        for member in self._members.values():
            member._stop_runs()
        return monotonic()

    async def _sweep(self) -> None:
        """End jobs past their lease or wait, and forget expired ones."""
        try:
            await self._backend.reap(limit=_SWEEP_LIMIT)
            await self._backend.purge(limit=_SWEEP_LIMIT)
        except Exception:
            logger.warning("Job store sweep failed", exc_info=True)

    async def _beat(self) -> Exception | None:
        """Extend each lease, write new counts, and read each cancel flag.

        Returns the store's error, or `None` when it answered.
        """
        async with self._round:
            beats = [
                beat
                for member in self._members.values()
                for beat in member._beats()
            ]
            if not beats:
                return None
            try:
                replies = await self._backend.beat(
                    runner=self.runner, beats=beats, lease=LEASE
                )
            except Exception as error:  # noqa: BLE001
                return error
            for reply in replies:
                self._members[reply.name]._read_reply(reply)
            return None

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
            try:
                claimed = await self._backend.claim(
                    slots=slots, runner=self.runner, lease=LEASE
                )
            except Exception:
                logger.debug("Job claim failed, retrying", exc_info=True)
                claimed = []
            for job in claimed:
                slots[job.record.name] -= 1
                self._members[job.record.name]._accept(job)
            for name, unused in slots.items():
                self._members[name]._give_back_slots(unused)


_loops: WeakKeyDictionary[JobBackend, JobStoreLoop] = WeakKeyDictionary()
"""The loop of each store this replica has open."""


def store_loop(backend: JobBackend, *, opens: bool = True) -> JobStoreLoop:
    """Return the loop of `backend`, a new one when it has none.

    `opens` says whether a new loop opens and closes the store. Only a store
    the job built is opened here. Whoever built any other store opens it.
    """
    loop = _loops.get(backend)
    if loop is None:
        loop = _loops[backend] = JobStoreLoop(backend, opens=opens)
    return loop
