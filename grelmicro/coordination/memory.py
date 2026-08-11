"""In-memory coordination adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, ClassVar, Self

from grelmicro.coordination._protocol import (
    LeaderRecord,
    LockBackend,
    ReadWriteLockBackend,
    ReadWriteLockState,
    ScheduleBackend,
    WriteGrant,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from grelmicro.types import BackendScope


class MemoryLockAdapter(LockBackend):
    """Memory Lock Adapter.

    This is not a backend with a real distributed lock. It is a local lock that can be used for
    testing purposes or for locking operations that are executed in the same asyncio event loop.
    """

    scope: ClassVar[BackendScope] = "process"
    """State lives in this process and is not shared beyond it."""

    def __init__(self) -> None:
        """Initialize the lock backend."""
        self._locks: dict[str, tuple[str | None, float]] = {}
        self._fences: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the lock backend."""
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lock backend."""
        self._locks.clear()
        self._fences.clear()

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire the lock, returning the fencing token or `None`."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        free = current_token is None or expire_at < monotonic()
        if free or current_token == token:
            if free:
                # Free-to-held transition: a new holder or a takeover of an
                # expired lock bumps the per-name high-water counter. The
                # counter persists for the adapter lifetime, even across
                # release, so re-acquire keeps climbing.
                self._fences[name] = self._fences.get(name, 0) + 1
            self._locks[name] = (token, monotonic() + duration)
            return self._fences[name]
        return None

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        if current_token == token and expire_at >= monotonic():
            del self._locks[name]
            return True
        if current_token and expire_at < monotonic():
            del self._locks[name]
        return False

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        return current_token is not None and expire_at >= monotonic()

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        current_token, expire_at = self._locks.get(name, (None, 0))
        return current_token == token and expire_at >= monotonic()


@dataclass(slots=True)
class _MemoryReadWriteState:
    """The state of one in-memory read-write lock."""

    generation: int = 0
    writer: str | None = None
    writer_expire_at: float = 0.0
    writer_expired: bool = False
    readers: dict[str, float] = field(default_factory=dict)
    intents: dict[str, float] = field(default_factory=dict)


class MemoryReadWriteLockAdapter(ReadWriteLockBackend):
    """Memory Read-Write Lock Adapter.

    Runs the same reader set, writer intent, and reaping algorithm as the
    distributed backends against a process-local dict. State disappears on
    restart and does not coordinate across nodes, so every process believes
    it holds the lock. Use a Redis, Postgres, SQLite, or Kubernetes backend
    across nodes.
    """

    scope: ClassVar[BackendScope] = "process"
    """State lives in this process and is not shared beyond it."""

    def __init__(self) -> None:
        """Initialize the read-write lock backend."""
        self._states: dict[str, _MemoryReadWriteState] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the read-write lock backend."""
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the read-write lock backend."""
        self._states.clear()

    def _reap(self, name: str) -> _MemoryReadWriteState:
        """Return the state for `name`, with every expired lease dropped."""
        state = self._states.get(name)
        if state is None:
            state = _MemoryReadWriteState()
            self._states[name] = state
        now = monotonic()
        if state.writer is not None and state.writer_expire_at < now:
            state.writer = None
            state.writer_expired = True
        for token, expire_at in list(state.readers.items()):
            if expire_at < now:
                del state.readers[token]
        for token, expire_at in list(state.intents.items()):
            if expire_at < now:
                del state.intents[token]
        return state

    async def acquire_read(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a read lease, returning the generation or `None`."""
        state = self._reap(name)
        if token in state.readers:
            state.readers[token] = monotonic() + duration
            return state.generation
        if state.writer is not None or state.intents:
            return None
        state.readers[token] = monotonic() + duration
        return state.generation

    async def acquire_write(
        self, *, name: str, token: str, duration: float, intent: bool = True
    ) -> WriteGrant | None:
        """Acquire the write lease, returning the grant or `None`."""
        state = self._reap(name)
        if state.writer == token:
            state.writer_expire_at = monotonic() + duration
            return WriteGrant(fencing_token=state.generation, poisoned=False)
        if state.writer is not None or state.readers:
            if intent:
                state.intents[token] = monotonic() + duration
            return None
        state.intents.pop(token, None)
        state.generation += 1
        state.writer = token
        state.writer_expire_at = monotonic() + duration
        poisoned = state.writer_expired
        state.writer_expired = False
        return WriteGrant(fencing_token=state.generation, poisoned=poisoned)

    async def release_read(self, *, name: str, token: str) -> bool:
        """Drop a read lease."""
        state = self._reap(name)
        return state.readers.pop(token, None) is not None

    async def release_write(self, *, name: str, token: str) -> bool:
        """Drop the write lease, leaving the lock clean."""
        state = self._reap(name)
        if state.writer != token:
            return False
        state.writer = None
        state.writer_expired = False
        return True

    async def cancel_intent(self, *, name: str, token: str) -> bool:
        """Withdraw a writer intent."""
        state = self._reap(name)
        return state.intents.pop(token, None) is not None

    async def downgrade(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Turn a held write lease into a read lease."""
        state = self._reap(name)
        if state.writer != token:
            return None
        state.writer = None
        state.writer_expired = False
        state.readers[token] = monotonic() + duration
        return state.generation

    async def state(self, *, name: str) -> ReadWriteLockState:
        """Return a point-in-time view of the lock."""
        state = self._reap(name)
        return ReadWriteLockState(
            generation=state.generation,
            writing=state.writer is not None,
            readers=len(state.readers),
            waiting_writers=len(state.intents),
        )

    async def owned_read(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds a live read lease."""
        return token in self._reap(name).readers

    async def owned_write(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds the live write lease."""
        return self._reap(name).writer == token


class MemoryScheduleAdapter(ScheduleBackend):
    """Memory Schedule Adapter.

    Stores `last_fired` epochs in a process-local dict guarded by an
    `asyncio.Lock` so `claim` is an atomic check-and-set within one event
    loop. State disappears on restart and does not coordinate across nodes,
    so it is for testing and single-process apps. Use a Redis, Postgres, or
    SQLite backend for durable distributed cron.
    """

    scope: ClassVar[BackendScope] = "process"
    """State lives in this process and is not shared beyond it."""

    def __init__(self) -> None:
        """Initialize an empty schedule store."""
        self._last_fired: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the schedule backend."""
        self._loop = asyncio.get_running_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the schedule backend."""
        self._last_fired.clear()

    async def claim(self, name: str, due: float) -> bool:
        """Atomically claim the fire at `due`."""
        async with self._lock:
            stored = self._last_fired.get(name)
            if stored is not None and stored >= due:
                return False
            self._last_fired[name] = due
            return True

    async def last_fired(self, name: str) -> float | None:
        """Return the stored `last_fired` epoch, or `None`."""
        return self._last_fired.get(name)


class MemoryLeaderElectionAdapter:
    """In-memory leader election adapter for tests and single-process apps.

    Stores the `LeaderRecord` in a process-local dict and runs the same
    acquire/renew/expire algorithm as the distributed backends. State
    disappears on restart and does not coordinate across nodes, so every
    process believes it leads. Use a Redis, Postgres, or Kubernetes backend
    for real elections.
    """

    scope: ClassVar[BackendScope] = "process"
    """State lives in this process and is not shared beyond it."""

    def __init__(self) -> None:
        """Initialize an empty record store."""
        self._records: dict[str, LeaderRecord] = {}

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

    def _live(self, name: str) -> LeaderRecord | None:
        """Return the record only while its lease is still valid."""
        record = self._records.get(name)
        if record is None:
            return None
        expires_at = record.renewed_at + timedelta(
            seconds=record.lease_duration
        )
        if datetime.now(UTC) >= expires_at:
            return None
        return record

    async def acquire_or_renew(
        self,
        *,
        name: str,
        token: str,
        duration: float,
        metadata: Mapping[str, str] | None = None,
    ) -> LeaderRecord:
        """Acquire or renew the lease, returning the resulting record."""
        now = datetime.now(UTC)
        meta = dict(metadata or {})
        live = self._live(name)
        if live is not None and live.holder != token:
            return live
        if live is not None:
            record = replace(
                live, renewed_at=now, lease_duration=duration, metadata=meta
            )
        else:
            previous = self._records.get(name)
            if previous is None or previous.holder == token:
                transitions = previous.transitions if previous else 0
            else:
                transitions = previous.transitions + 1
            record = LeaderRecord(
                holder=token,
                lease_duration=duration,
                acquired_at=now,
                renewed_at=now,
                transitions=transitions,
                metadata=meta,
            )
        self._records[name] = record
        return record

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lease when held by `token`."""
        live = self._live(name)
        if live is not None and live.holder == token:
            del self._records[name]
            return True
        return False

    async def get(self, *, name: str) -> LeaderRecord | None:
        """Return the current live record, or `None`."""
        return self._live(name)
