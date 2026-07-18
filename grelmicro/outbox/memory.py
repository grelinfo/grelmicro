"""In-memory Outbox Adapter.

For tests and single-process apps. Messages live in the process and are lost
on restart, so there is no transaction to join: `enqueue` stores the message
immediately and ignores the handle.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from uuid import UUID

    from grelmicro.outbox._message import OutboxRecord


@dataclass
class _Row:
    """A stored message and its delivery state."""

    record: OutboxRecord
    state: str
    attempts: int
    available_at: datetime
    created_at: datetime
    last_error: str | None = None


class MemoryOutboxAdapter:
    """In-memory outbox storage backend."""

    def __init__(self) -> None:
        """Initialize an empty in-memory backend."""
        self._rows: dict[UUID, _Row] = {}
        self._wake = asyncio.Event()

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

    async def enqueue(self, handle: Any, record: OutboxRecord) -> bool:  # noqa: ANN401, ARG002
        """Store a record, skipping it on a duplicate `dedup_key`.

        A stored `dedup_key` blocks a re-publish regardless of state, matching
        the Postgres unique partial index, until the row is deleted.
        """
        if record.dedup_key is not None and any(
            row.record.dedup_key == record.dedup_key
            for row in self._rows.values()
        ):
            return False
        now = _now()
        self._rows[record.id] = _Row(
            record=record,
            state="pending",
            attempts=0,
            available_at=record.available_at or now,
            created_at=now,
        )
        self._wake.set()
        return True

    async def claim(
        self, *, topics: Sequence[str], limit: int, lease: float
    ) -> list[OutboxRecord]:
        """Claim up to `limit` due messages for the given topics."""
        now = _now()
        due = sorted(
            (
                row
                for row in self._rows.values()
                if row.record.topic in set(topics)
                and row.state in {"pending", "processing"}
                and row.available_at <= now
            ),
            key=lambda row: (row.available_at, row.record.id),
        )[:limit]
        claimed: list[OutboxRecord] = []
        for row in due:
            row.state = "processing"
            row.attempts += 1
            row.available_at = now + timedelta(seconds=lease)
            claimed.append(replace(row.record, attempts=row.attempts))
        return claimed

    async def complete(
        self, *, message_id: UUID, attempts: int, keep: bool
    ) -> None:
        """Mark a message delivered, fencing on the claimed attempt count."""
        row = self._rows.get(message_id)
        if row is None or row.attempts != attempts:
            return
        if keep:
            row.state = "delivered"
            row.last_error = None
        else:
            self._rows.pop(message_id, None)

    async def reschedule(
        self,
        *,
        message_id: UUID,
        attempts: int,
        delay: float,
        error: str,
        dead: bool,
    ) -> None:
        """Reschedule a failed message or dead-letter it, fenced on attempts."""
        row = self._rows.get(message_id)
        if row is None or row.attempts != attempts:
            return
        row.last_error = error
        if dead:
            row.state = "dead"
        else:
            row.state = "pending"
            row.available_at = _now() + timedelta(seconds=delay)

    async def redrive(self, *, topic: str | None = None) -> int:
        """Move dead messages back to pending. Returns the count moved."""
        moved = 0
        for row in self._rows.values():
            if row.state == "dead" and (
                topic is None or row.record.topic == topic
            ):
                row.state = "pending"
                row.attempts = 0
                row.available_at = _now()
                row.last_error = None
                moved += 1
        if moved:
            self._wake.set()
        return moved

    async def purge(self, *, before_seconds: float | None = None) -> int:
        """Delete delivered and dead rows. Returns the count removed."""
        cutoff = (
            _now() - timedelta(seconds=before_seconds)
            if before_seconds is not None
            else None
        )
        doomed = [
            message_id
            for message_id, row in self._rows.items()
            if row.state in {"delivered", "dead"}
            and (cutoff is None or row.created_at < cutoff)
        ]
        for message_id in doomed:
            del self._rows[message_id]
        return len(doomed)

    async def wait_notify(self, *, timeout: float) -> None:  # noqa: ASYNC109
        """Return when a message is signalled or `timeout` elapses."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)
