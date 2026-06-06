"""In-memory leader election backend."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

from grelmicro.coordination.abc import LeaderRecord

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType


class MemoryLeaderElectionBackend:
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
