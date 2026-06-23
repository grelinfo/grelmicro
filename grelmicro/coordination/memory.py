"""In-memory coordination adapters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Self

from grelmicro.coordination.abc import (
    LeaderRecord,
    LockBackend,
    ScheduleBackend,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


class MemoryLockAdapter(LockBackend):
    """Memory Lock Adapter.

    This is not a backend with a real distributed lock. It is a local lock that can be used for
    testing purposes or for locking operations that are executed in the same asyncio event loop.
    """

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


class MemoryScheduleAdapter(ScheduleBackend):
    """Memory Schedule Adapter.

    Stores `last_fired` epochs in a process-local dict guarded by an
    `asyncio.Lock` so `claim` is an atomic check-and-set within one event
    loop. State disappears on restart and does not coordinate across nodes,
    so it is for testing and single-process apps. Use a Redis, Postgres, or
    SQLite backend for durable distributed cron.
    """

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
    """In-memory leader election backend for tests and single-process apps.

    Stores the `LeaderRecord` in a process-local dict and runs the same
    acquire/renew/expire algorithm as the distributed backends. State
    disappears on restart and does not coordinate across nodes, so every
    process believes it leads. Use a Redis, Postgres, or Kubernetes backend
    for real elections.
    """

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
