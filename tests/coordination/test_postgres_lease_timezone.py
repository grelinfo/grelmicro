"""Postgres coordination leases shared by sessions in different time zones.

One replica's sessions run in `UTC` and another's in `Europe/Zurich`. A
lease one of them takes must hold for the other for its whole duration.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from grelmicro.coordination.postgres import (
    PostgresLeaderElectionAdapter,
    PostgresLockAdapter,
    PostgresReadWriteLockAdapter,
)
from grelmicro.providers.postgres import PostgresProvider

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

DURATION = timedelta(seconds=60)


class _OldPostgresLockAdapter(PostgresLockAdapter):
    """The lock adapter as earlier releases shipped it, with a `TIMESTAMP` expiry."""

    _SQL_CREATE_TABLE_IF_NOT_EXISTS = """
        CREATE TABLE IF NOT EXISTS {table_name} (
            name TEXT PRIMARY KEY,
            token TEXT,
            expire_at TIMESTAMP,
            fence BIGINT NOT NULL DEFAULT 0
        );
        ALTER TABLE {table_name}
            ADD COLUMN IF NOT EXISTS fence BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE {table_name} ALTER COLUMN token DROP NOT NULL;
        ALTER TABLE {table_name} ALTER COLUMN expire_at DROP NOT NULL;
    """

    _SQL_MIGRATE_EXPIRE_AT_TIMESTAMPTZ = "SELECT 1;"


_SQL_EXPIRE_AT_TYPE = """
    SELECT format_type(atttypid, atttypmod) FROM pg_attribute
    WHERE attrelid = $1::regclass AND attname = 'expire_at';
"""

_SQL_FILENODE = "SELECT pg_relation_filenode($1::regclass);"


@pytest.fixture(scope="module")
async def url() -> AsyncGenerator[str]:
    """Start a Postgres container and return its connection URL."""
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        yield f"postgresql://test:test@localhost:{port}/test"


@pytest.fixture(scope="module")
async def utc(url: str) -> AsyncGenerator[PostgresProvider]:
    """Provide a replica whose sessions run in `UTC`."""
    async with PostgresProvider(f"{url}?timezone=UTC") as provider:
        yield provider


@pytest.fixture(scope="module")
async def zurich(url: str) -> AsyncGenerator[PostgresProvider]:
    """Provide a replica whose sessions run in `Europe/Zurich`."""
    async with PostgresProvider(f"{url}?timezone=Europe/Zurich") as provider:
        yield provider


@pytest.fixture
async def table_name(utc: PostgresProvider) -> AsyncGenerator[str]:
    """Return a fresh lock table name and drop the table afterwards."""
    name = f"locks_{uuid4().hex}"
    yield name
    await utc.client.execute(f"DROP TABLE IF EXISTS {name};")


async def _old_lease(
    provider: PostgresProvider, table_name: str
) -> tuple[str, str]:
    """Take a lock lease with the earlier release and return its name and token."""
    name = uuid4().hex
    token = uuid4().hex
    async with _OldPostgresLockAdapter(
        provider=provider, table_name=table_name
    ) as old:
        await old.acquire(name=name, token=token, duration=DURATION)
    return name, token


async def _expire_at(
    provider: PostgresProvider, table_name: str, name: str
) -> datetime:
    """Return the stored expiry of a lock as an instant."""
    return await provider.client.fetchval(
        f"SELECT expire_at::timestamptz FROM {table_name} WHERE name = $1;",  # noqa: S608
        name,
    )


@pytest.fixture(params=["utc-zurich", "zurich-utc"])
def replicas(
    request: pytest.FixtureRequest,
    utc: PostgresProvider,
    zurich: PostgresProvider,
) -> tuple[PostgresProvider, PostgresProvider]:
    """Return a holder replica and another replica in the other time zone."""
    if request.param == "utc-zurich":
        return utc, zurich
    return zurich, utc


async def test_postgres_providers_session_time_zones_differ(
    utc: PostgresProvider, zurich: PostgresProvider
) -> None:
    """The two replicas run their sessions in different time zones."""
    # Act
    utc_zone = await utc.client.fetchval("SHOW TimeZone;")
    zurich_zone = await zurich.client.fetchval("SHOW TimeZone;")

    # Assert
    assert utc_zone == "UTC"
    assert zurich_zone == "Europe/Zurich"


async def test_postgres_lock_held_in_other_time_zone_blocks_acquire(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A lock held from one time zone stays held for another."""
    # Arrange
    holder = PostgresLockAdapter(provider=replicas[0])
    other = PostgresLockAdapter(provider=replicas[1])
    name = uuid4().hex
    async with holder, other:
        await holder.acquire(name=name, token=uuid4().hex, duration=DURATION)

        # Act
        fence = await other.acquire(
            name=name, token=uuid4().hex, duration=DURATION
        )
        locked = await other.locked(name=name)

    # Assert
    assert fence is None
    assert locked is True


async def test_postgres_lock_released_in_other_time_zone_frees_lock(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A lock released from one time zone is free for another."""
    # Arrange
    holder = PostgresLockAdapter(provider=replicas[0])
    other = PostgresLockAdapter(provider=replicas[1])
    name = uuid4().hex
    token = uuid4().hex
    async with holder, other:
        await holder.acquire(name=name, token=token, duration=DURATION)

        # Act
        released = await holder.release(name=name, token=token)
        fence = await other.acquire(
            name=name, token=uuid4().hex, duration=DURATION
        )

    # Assert
    assert released is True
    assert fence is not None


async def test_postgres_lock_expired_in_other_time_zone_frees_lock(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A lock lease taken from one time zone is free for another once it ends."""
    # Arrange
    holder = PostgresLockAdapter(provider=replicas[0])
    other = PostgresLockAdapter(provider=replicas[1])
    name = uuid4().hex
    async with holder, other:
        await holder.acquire(
            name=name, token=uuid4().hex, duration=timedelta(milliseconds=50)
        )
        await asyncio.sleep(0.1)

        # Act
        locked = await other.locked(name=name)
        fence = await other.acquire(
            name=name, token=uuid4().hex, duration=DURATION
        )

    # Assert
    assert locked is False
    assert fence is not None


async def test_postgres_rwlock_reader_in_other_time_zone_blocks_writer(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A read lease held from one time zone blocks a writer in another."""
    # Arrange
    holder = PostgresReadWriteLockAdapter(provider=replicas[0])
    other = PostgresReadWriteLockAdapter(provider=replicas[1])
    name = uuid4().hex
    async with holder, other:
        await holder.acquire_read(
            name=name, token=uuid4().hex, duration=DURATION
        )

        # Act
        grant = await other.acquire_write(
            name=name, token=uuid4().hex, duration=DURATION, intent=False
        )

    # Assert
    assert grant is None


async def test_postgres_rwlock_writer_in_other_time_zone_blocks_reader(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A write lease held from one time zone blocks a reader in another."""
    # Arrange
    holder = PostgresReadWriteLockAdapter(provider=replicas[0])
    other = PostgresReadWriteLockAdapter(provider=replicas[1])
    name = uuid4().hex
    async with holder, other:
        await holder.acquire_write(
            name=name, token=uuid4().hex, duration=DURATION
        )

        # Act
        generation = await other.acquire_read(
            name=name, token=uuid4().hex, duration=DURATION
        )

    # Assert
    assert generation is None


async def test_postgres_rwlock_intent_in_other_time_zone_blocks_reader(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A writer intent left from one time zone blocks a new reader in another."""
    # Arrange
    holder = PostgresReadWriteLockAdapter(provider=replicas[0])
    other = PostgresReadWriteLockAdapter(provider=replicas[1])
    name = uuid4().hex
    async with holder, other:
        await other.acquire_read(
            name=name, token=uuid4().hex, duration=DURATION
        )
        await holder.acquire_write(
            name=name, token=uuid4().hex, duration=DURATION
        )

        # Act
        generation = await other.acquire_read(
            name=name, token=uuid4().hex, duration=DURATION
        )

    # Assert
    assert generation is None


async def test_postgres_leader_lease_in_other_time_zone_blocks_takeover(
    replicas: tuple[PostgresProvider, PostgresProvider],
) -> None:
    """A leader lease held from one time zone is not taken over from another."""
    # Arrange
    holder = PostgresLeaderElectionAdapter(provider=replicas[0])
    other = PostgresLeaderElectionAdapter(provider=replicas[1])
    name = uuid4().hex
    leader = uuid4().hex
    async with holder, other:
        await holder.acquire_or_renew(
            name=name, token=leader, duration=DURATION
        )

        # Act
        record = await other.acquire_or_renew(
            name=name, token=uuid4().hex, duration=DURATION
        )
        current = await other.get(name=name)

    # Assert
    assert record.holder == leader
    assert current is not None
    assert current.holder == leader


async def test_postgres_lock_setup_migrates_expire_at_to_timestamptz(
    utc: PostgresProvider, table_name: str
) -> None:
    """Setup turns an old `TIMESTAMP` expiry column into `TIMESTAMPTZ`."""
    # Arrange
    await _old_lease(utc, table_name)

    # Act
    async with PostgresLockAdapter(provider=utc, table_name=table_name):
        pass

    # Assert
    column_type = await utc.client.fetchval(_SQL_EXPIRE_AT_TYPE, table_name)
    assert column_type == "timestamp with time zone"


async def test_postgres_lock_setup_migration_in_utc_session_keeps_table_file(
    utc: PostgresProvider, table_name: str
) -> None:
    """In a `UTC` session the expiry column changes without a table rewrite."""
    # Arrange
    await _old_lease(utc, table_name)
    before = await utc.client.fetchval(_SQL_FILENODE, table_name)

    # Act
    async with PostgresLockAdapter(provider=utc, table_name=table_name):
        pass

    # Assert
    assert await utc.client.fetchval(_SQL_FILENODE, table_name) == before


async def test_postgres_lock_setup_migration_keeps_live_lease_held(
    replicas: tuple[PostgresProvider, PostgresProvider], table_name: str
) -> None:
    """A lease written before the migration ends at the same instant after it."""
    # Arrange
    provider = replicas[0]
    name, token = await _old_lease(provider, table_name)
    expected = await _expire_at(provider, table_name, name)

    # Act
    async with PostgresLockAdapter(
        provider=provider, table_name=table_name
    ) as backend:
        owned = await backend.owned(name=name, token=token)
        fence = await backend.acquire(
            name=name, token=uuid4().hex, duration=DURATION
        )

    # Assert
    assert owned is True
    assert fence is None
    assert await _expire_at(provider, table_name, name) == expected


async def test_postgres_lock_setup_migration_from_other_time_zone_shifts_lease(
    utc: PostgresProvider, zurich: PostgresProvider, table_name: str
) -> None:
    """A lease written in `UTC` and migrated from `Europe/Zurich` moves by the offset."""
    # Arrange
    name, token = await _old_lease(utc, table_name)
    written = await _expire_at(utc, table_name, name)
    offset = written.astimezone(ZoneInfo("Europe/Zurich")).utcoffset()
    assert offset is not None

    # Act
    async with PostgresLockAdapter(
        provider=zurich, table_name=table_name
    ) as backend:
        owned = await backend.owned(name=name, token=token)
        migrated = await _expire_at(utc, table_name, name)

    # Assert
    assert migrated == written - offset
    assert owned is False


async def test_postgres_lock_setup_migration_runs_once(
    utc: PostgresProvider, table_name: str
) -> None:
    """A second setup leaves a migrated table and its leases untouched."""
    # Arrange
    name, token = await _old_lease(utc, table_name)
    async with PostgresLockAdapter(provider=utc, table_name=table_name):
        pass
    before = await _expire_at(utc, table_name, name)
    filenode = await utc.client.fetchval(_SQL_FILENODE, table_name)

    # Act
    async with PostgresLockAdapter(
        provider=utc, table_name=table_name
    ) as backend:
        owned = await backend.owned(name=name, token=token)

    # Assert
    column_type = await utc.client.fetchval(_SQL_EXPIRE_AT_TYPE, table_name)
    assert column_type == "timestamp with time zone"
    assert owned is True
    assert await _expire_at(utc, table_name, name) == before
    assert await utc.client.fetchval(_SQL_FILENODE, table_name) == filenode


async def test_postgres_lock_setup_with_table_in_use_skips_migration(
    url: str,
    utc: PostgresProvider,
    table_name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup keeps the old column when the table lock is not granted in time."""
    # Arrange
    name, token = await _old_lease(utc, table_name)
    reader = await asyncpg.connect(url)
    try:
        transaction = reader.transaction()
        await transaction.start()
        await reader.execute(f"SELECT 1 FROM {table_name};")  # noqa: S608

        # Act
        with caplog.at_level(logging.WARNING, "grelmicro.coordination"):
            async with PostgresLockAdapter(
                provider=utc, table_name=table_name
            ) as backend:
                owned = await backend.owned(name=name, token=token)
        await transaction.rollback()
    finally:
        await reader.close()

    # Assert
    column_type = await utc.client.fetchval(_SQL_EXPIRE_AT_TYPE, table_name)
    assert column_type == "timestamp without time zone"
    assert owned is True
    assert "keeps its TIMESTAMP expiry column" in caplog.text


async def test_postgres_lock_setup_after_skipped_migration_migrates(
    url: str, utc: PostgresProvider, table_name: str
) -> None:
    """The next setup migrates a column an earlier setup had to keep."""
    # Arrange
    name, token = await _old_lease(utc, table_name)
    reader = await asyncpg.connect(url)
    try:
        async with reader.transaction():
            await reader.execute(f"SELECT 1 FROM {table_name};")  # noqa: S608
            async with PostgresLockAdapter(provider=utc, table_name=table_name):
                pass
    finally:
        await reader.close()

    # Act
    async with PostgresLockAdapter(
        provider=utc, table_name=table_name
    ) as backend:
        owned = await backend.owned(name=name, token=token)

    # Assert
    column_type = await utc.client.fetchval(_SQL_EXPIRE_AT_TYPE, table_name)
    assert column_type == "timestamp with time zone"
    assert owned is True


async def test_postgres_lock_old_worker_keeps_lock_across_migration(
    url: str, table_name: str
) -> None:
    """A worker on the old table keeps its lock after a new one migrates it."""
    # Arrange
    name = uuid4().hex
    token = uuid4().hex
    zurich_url = f"{url}?timezone=Europe/Zurich"
    async with (
        PostgresProvider(zurich_url) as old_provider,
        _OldPostgresLockAdapter(
            provider=old_provider, table_name=table_name
        ) as old,
    ):
        await old.acquire(name=name, token=token, duration=DURATION)
        await old.owned(name=name, token=token)
        async with (
            PostgresProvider(zurich_url) as new_provider,
            PostgresLockAdapter(provider=new_provider, table_name=table_name),
        ):
            pass

        # Act
        owned = await old.owned(name=name, token=token)
        fence = await old.acquire(
            name=name, token=uuid4().hex, duration=DURATION
        )

    # Assert
    assert owned is True
    assert fence is None
