"""Backend-level tests for the in-memory adapter.

These pin the semantics the relay depends on and that the Postgres backend
must match: claim increments attempts, settles are fenced on the claimed
attempt count, and a stored `dedup_key` blocks a re-publish in any state.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from grelmicro.outbox._message import OutboxRecord
from grelmicro.outbox._registry import OutboxRegistry
from grelmicro.outbox._uuid import uuid7
from grelmicro.outbox.errors import HandlerNotFoundError
from grelmicro.outbox.memory import MemoryOutboxAdapter, _now

pytestmark = [pytest.mark.timeout(5)]


def _record(*, dedup_key: str | None = None) -> OutboxRecord:
    """Return a minimal record."""
    return OutboxRecord(
        id=uuid7(), topic="job", payload={"n": 1}, dedup_key=dedup_key
    )


def test_registry_get_unknown_topic_raises() -> None:
    """Resolving an unregistered topic raises `HandlerNotFoundError`."""
    with pytest.raises(HandlerNotFoundError):
        OutboxRegistry().get("missing")


async def test_claim_increments_attempts() -> None:
    """Claiming a record bumps its attempt count to 1."""
    backend = MemoryOutboxAdapter()
    record = _record()
    await backend.enqueue(None, record)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    assert claimed.id == record.id
    assert claimed.attempts == 1


async def test_claim_filters_unregistered_topics() -> None:
    """Only the requested topics are claimed."""
    backend = MemoryOutboxAdapter()
    await backend.enqueue(None, _record())
    assert await backend.claim(topics=["other"], limit=10, lease=60) == []


async def test_complete_fenced_on_attempts() -> None:
    """A settle with a stale attempt count is ignored."""
    backend = MemoryOutboxAdapter()
    record = _record()
    await backend.enqueue(None, record)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)

    await backend.complete(message_id=claimed.id, attempts=999, keep=False)
    assert record.id in backend._rows

    await backend.complete(
        message_id=claimed.id, attempts=claimed.attempts, keep=False
    )
    assert record.id not in backend._rows


async def test_dedup_blocks_in_any_state() -> None:
    """A stored dedup_key blocks a re-publish even after the row is dead."""
    backend = MemoryOutboxAdapter()
    record = _record(dedup_key="k")
    assert await backend.enqueue(None, record) is True

    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.reschedule(
        message_id=claimed.id,
        attempts=claimed.attempts,
        delay=0,
        error="boom",
        dead=True,
    )
    assert backend._rows[record.id].state == "dead"

    assert await backend.enqueue(None, _record(dedup_key="k")) is False


async def test_reschedule_fenced_on_attempts() -> None:
    """A reschedule with a stale attempt count is ignored."""
    backend = MemoryOutboxAdapter()
    record = _record()
    await backend.enqueue(None, record)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.reschedule(
        message_id=claimed.id,
        attempts=claimed.attempts + 99,
        delay=0,
        error="stale",
        dead=False,
    )
    assert backend._rows[record.id].state == "processing"


async def test_redrive_skips_non_dead_and_counts_zero() -> None:
    """Redrive leaves non-dead rows and returns zero when none are dead."""
    backend = MemoryOutboxAdapter()
    await backend.enqueue(None, _record())
    assert await backend.redrive() == 0
    assert backend._rows[next(iter(backend._rows))].state == "pending"


async def test_redrive_moves_dead_back_to_pending() -> None:
    """Redrive resets dead rows so they are claimable again."""
    backend = MemoryOutboxAdapter()
    record = _record()
    await backend.enqueue(None, record)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.reschedule(
        message_id=claimed.id,
        attempts=claimed.attempts,
        delay=0,
        error="boom",
        dead=True,
    )

    assert await backend.redrive(topic="job") == 1
    (reclaimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    assert reclaimed.id == record.id


async def test_purge_removes_terminal_rows_only() -> None:
    """Purge deletes delivered and dead rows and leaves pending ones."""
    backend = MemoryOutboxAdapter()

    delivered = _record()
    await backend.enqueue(None, delivered)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.complete(
        message_id=claimed.id, attempts=claimed.attempts, keep=True
    )

    dead = _record()
    await backend.enqueue(None, dead)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.reschedule(
        message_id=claimed.id,
        attempts=claimed.attempts,
        delay=0,
        error="boom",
        dead=True,
    )

    pending = _record()
    await backend.enqueue(None, pending)

    removed = await backend.purge()
    assert removed == 2  # noqa: PLR2004
    assert set(backend._rows) == {pending.id}


async def test_purge_states_filter_targets_delivered_only() -> None:
    """Purging with `states=("delivered",)` leaves dead rows in place."""
    backend = MemoryOutboxAdapter()

    delivered = _record()
    await backend.enqueue(None, delivered)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.complete(
        message_id=claimed.id, attempts=claimed.attempts, keep=True
    )

    dead = _record()
    await backend.enqueue(None, dead)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.reschedule(
        message_id=claimed.id,
        attempts=claimed.attempts,
        delay=0,
        error="boom",
        dead=True,
    )

    removed = await backend.purge(states=("delivered",))
    assert removed == 1
    assert set(backend._rows) == {dead.id}


async def test_purge_measures_delivered_age_from_delivery_time() -> None:
    """A delivered row ages from delivery, not from when it was staged."""
    backend = MemoryOutboxAdapter()
    record = _record()
    await backend.enqueue(None, record)
    # Backdate creation far into the past.
    backend._rows[record.id].created_at = _now() - timedelta(days=365)
    (claimed,) = await backend.claim(topics=["job"], limit=10, lease=60)
    await backend.complete(
        message_id=claimed.id, attempts=claimed.attempts, keep=True
    )

    # Freshly delivered, so a one-hour window keeps it, even though a
    # creation-based purge would have deleted the year-old row.
    assert await backend.purge(before_seconds=3600, states=("delivered",)) == 0
    assert record.id in backend._rows
