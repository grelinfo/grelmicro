"""Postgres coordination leases across a daylight saving time change.

Every session runs in `Europe/Zurich`. Each lease is long enough to cross
the next change of the zone's UTC offset, and must still expire exactly
its duration later in absolute time.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from grelmicro.coordination.postgres import (
    PostgresLeaderElectionAdapter,
    PostgresLockAdapter,
    PostgresReadWriteLockAdapter,
)
from grelmicro.providers.postgres import PostgresProvider

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

_SQL_DAYS_ACROSS_NEXT_OFFSET_CHANGE = """
    SELECT min(d) FROM generate_series(1, 400) AS d
    WHERE extract(timezone FROM NOW() + make_interval(days => d))
        <> extract(timezone FROM NOW());
"""
"""The fewest whole days from now that land on another UTC offset."""


@pytest.fixture(scope="module")
async def provider() -> AsyncGenerator[PostgresProvider]:
    """Provide a provider whose sessions run in `Europe/Zurich`."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        conn = await asyncpg.connect(url)
        try:
            await conn.execute(
                "ALTER DATABASE test SET TimeZone = 'Europe/Zurich'"
            )
        finally:
            await conn.close()
        async with PostgresProvider(url) as provider:
            yield provider


@pytest.fixture
async def duration(provider: PostgresProvider) -> timedelta:
    """Return whole days that cross the next UTC offset change in Zurich."""
    days = await provider.client.fetchval(_SQL_DAYS_ACROSS_NEXT_OFFSET_CHANGE)
    return timedelta(days=days)


async def _clock(provider: PostgresProvider) -> datetime:
    """Return the database clock."""
    return await provider.client.fetchval("SELECT clock_timestamp();")


async def test_postgres_lock_lease_across_dst_change_expires_after_exact_duration(
    provider: PostgresProvider, duration: timedelta
) -> None:
    """A lock lease across an offset change expires its duration later."""
    # Arrange
    name = uuid4().hex
    async with PostgresLockAdapter(provider=provider) as backend:
        before = await _clock(provider)

        # Act
        await backend.acquire(name=name, token=uuid4().hex, duration=duration)
        after = await _clock(provider)

    # Assert
    expire_at = await provider.client.fetchval(
        "SELECT expire_at::timestamptz FROM locks WHERE name = $1;", name
    )
    assert before + duration <= expire_at <= after + duration


async def test_postgres_rwlock_read_lease_across_dst_change_expires_after_exact_duration(
    provider: PostgresProvider, duration: timedelta
) -> None:
    """A read lease across an offset change expires its duration later."""
    # Arrange
    name = uuid4().hex
    async with PostgresReadWriteLockAdapter(provider=provider) as backend:
        before = await _clock(provider)

        # Act
        await backend.acquire_read(
            name=name, token=uuid4().hex, duration=duration
        )
        after = await _clock(provider)

    # Assert
    expire_at = await provider.client.fetchval(
        "SELECT expire_at FROM grelmicro_rwlocks_holders WHERE name = $1;", name
    )
    assert before + duration <= expire_at <= after + duration


async def test_postgres_rwlock_write_lease_across_dst_change_expires_after_exact_duration(
    provider: PostgresProvider, duration: timedelta
) -> None:
    """A write lease across an offset change expires its duration later."""
    # Arrange
    name = uuid4().hex
    async with PostgresReadWriteLockAdapter(provider=provider) as backend:
        before = await _clock(provider)

        # Act
        await backend.acquire_write(
            name=name, token=uuid4().hex, duration=duration
        )
        after = await _clock(provider)

    # Assert
    expire_at = await provider.client.fetchval(
        "SELECT writer_expire_at FROM grelmicro_rwlocks WHERE name = $1;", name
    )
    assert before + duration <= expire_at <= after + duration


async def test_postgres_rwlock_write_intent_across_dst_change_expires_after_exact_duration(
    provider: PostgresProvider, duration: timedelta
) -> None:
    """A writer intent across an offset change expires its duration later."""
    # Arrange
    name = uuid4().hex
    async with PostgresReadWriteLockAdapter(provider=provider) as backend:
        await backend.acquire_read(
            name=name, token=uuid4().hex, duration=timedelta(seconds=30)
        )
        before = await _clock(provider)

        # Act
        await backend.acquire_write(
            name=name, token=uuid4().hex, duration=duration
        )
        after = await _clock(provider)

    # Assert
    expire_at = await provider.client.fetchval(
        "SELECT expire_at FROM grelmicro_rwlocks_holders WHERE name = $1 AND kind = 'i';",
        name,
    )
    assert before + duration <= expire_at <= after + duration


async def test_postgres_rwlock_downgrade_across_dst_change_expires_after_exact_duration(
    provider: PostgresProvider, duration: timedelta
) -> None:
    """A downgraded read lease across an offset change expires its duration later."""
    # Arrange
    name = uuid4().hex
    token = uuid4().hex
    async with PostgresReadWriteLockAdapter(provider=provider) as backend:
        await backend.acquire_write(
            name=name, token=token, duration=timedelta(seconds=30)
        )
        before = await _clock(provider)

        # Act
        await backend.downgrade(name=name, token=token, duration=duration)
        after = await _clock(provider)

    # Assert
    expire_at = await provider.client.fetchval(
        "SELECT expire_at FROM grelmicro_rwlocks_holders WHERE name = $1 AND kind = 'r';",
        name,
    )
    assert before + duration <= expire_at <= after + duration


async def test_postgres_leader_lease_across_dst_change_expires_after_exact_duration(
    provider: PostgresProvider, duration: timedelta
) -> None:
    """A leader lease across an offset change expires its duration later."""
    # Arrange
    name = uuid4().hex
    async with PostgresLeaderElectionAdapter(provider=provider) as backend:
        before = await _clock(provider)

        # Act
        record = await backend.acquire_or_renew(
            name=name, token=uuid4().hex, duration=duration
        )
        after = await _clock(provider)

    # Assert
    expire_at = await provider.client.fetchval(
        "SELECT renewed_at + make_interval(secs => lease_duration)"
        " FROM grelmicro_leader_election WHERE name = $1;",
        name,
    )
    assert record.lease_duration == duration
    assert before + duration <= expire_at <= after + duration
