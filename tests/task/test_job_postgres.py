"""Test what only the Postgres job backend promises."""

import asyncio
import secrets
from collections.abc import AsyncGenerator, Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.task import JobStatus
from grelmicro.task._protocol import Beat, JobBackend
from grelmicro.task.postgres import PostgresJobAdapter, _rows_affected
from tests._future import DEFAULT_TABLE_NAMES

pytestmark = [pytest.mark.timeout(30, func_only=True)]

NAME = "app:job"
LEASE = timedelta(seconds=30)
URL = "postgresql://test_user:test_password@test_host:1234/test_db"


def _submit_args(job_id: str, **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return the arguments of a `submit` that stores a plain queued job."""
    return {
        "name": NAME,
        "job_id": job_id,
        "owner": None,
        "payload": '{"a": 1}',
        "context": {"trace": "t"},
        "max_queued": None,
        "max_wait": None,
        "max_run": None,
        "keep": timedelta(hours=1),
        "max_kept_succeeded": None,
        "max_kept_failed": None,
        "start_as": None,
        "lease": LEASE,
        **overrides,
    }


def test_job_postgres_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    # Arrange
    monkeypatch.setenv("JOBS_POSTGRES_URL", URL)

    # Act
    backend = PostgresJobAdapter(env_prefix="JOBS_POSTGRES_")

    # Assert
    assert backend.provider.env_prefix == "JOBS_POSTGRES_"
    assert backend._owns_provider is True


def test_job_postgres_adapter_borrows_an_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    # Arrange
    provider = PostgresProvider(URL)

    # Act
    backend = PostgresJobAdapter(provider=provider)

    # Assert
    assert backend.provider is provider
    assert backend._owns_provider is False


def test_job_postgres_provider_factory_returns_the_adapter() -> None:
    """`PostgresProvider.job_backend()` returns a bound adapter."""
    # Arrange
    provider = PostgresProvider(URL)

    # Act
    backend = provider.job_backend(table_name="jobs")

    # Assert
    assert isinstance(backend, PostgresJobAdapter)
    assert isinstance(backend, JobBackend)
    assert backend.provider is provider
    assert backend._table_name == "jobs"
    assert PostgresJobAdapter.scope == "cluster"


@DEFAULT_TABLE_NAMES
def test_job_postgres_coordination_resolves_the_job_store() -> None:
    """A `Coordination` on a `PostgresProvider` holds its jobs there."""
    # Arrange
    provider = PostgresProvider(URL)

    # Act
    coordination = Coordination(provider)

    # Assert
    assert isinstance(coordination.job_backend, PostgresJobAdapter)


def test_job_postgres_rebind_provider_borrows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    # Arrange
    monkeypatch.setenv("POSTGRES_URL", URL)
    backend = PostgresJobAdapter()
    other = PostgresProvider(URL)

    # Act
    backend._rebind_provider(other)

    # Assert
    assert backend.provider is other
    assert backend._owns_provider is False


@pytest.mark.parametrize("table_name", ["1jobs", "jobs; DROP", "jobs-x", ""])
def test_job_postgres_table_name_must_be_an_identifier(table_name: str) -> None:
    """A table name that is not a SQL identifier is refused."""
    # Act and assert
    with pytest.raises(SettingsValidationError, match="not a valid SQL"):
        PostgresJobAdapter(
            provider=PostgresProvider(URL), table_name=table_name
        )


def test_job_postgres_statements_name_the_table() -> None:
    """Every statement reads and writes the table the adapter was given."""
    # Act
    backend = PostgresJobAdapter(
        provider=PostgresProvider(URL), table_name="my_jobs"
    )

    # Assert
    for key, sql in backend._sql.items():
        assert "{" not in sql, key
        if key not in {"now", "lock_name"}:
            assert "my_jobs" in sql, key
    assert "grelmicro_jobs" not in " ".join(backend._sql.values())


def test_job_postgres_claim_skips_rows_another_move_holds() -> None:
    """Claims, reaps and purges pass over rows another transaction locked."""
    # Act
    backend = PostgresJobAdapter(provider=PostgresProvider(URL))

    # Assert
    for key in ("claim", "catch_up_queue", "reap", "purge"):
        assert "FOR UPDATE SKIP LOCKED" in backend._sql[key], key


def test_job_postgres_rows_affected_parses_the_command_status() -> None:
    """The count comes from the last word of the command status."""
    # Act and assert
    assert _rows_affected("UPDATE 3") == 3  # noqa: PLR2004
    assert _rows_affected("DELETE 0") == 0
    assert _rows_affected("") == 0


async def test_job_postgres_owned_provider_is_opened_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When owned, the adapter opens and closes its provider."""
    # Arrange
    monkeypatch.setenv("POSTGRES_URL", URL)
    backend = PostgresJobAdapter()
    provider = MagicMock()
    provider.__aenter__ = AsyncMock()
    provider.__aexit__ = AsyncMock()
    backend._provider = provider

    # Act
    async with backend:
        pass

    # Assert
    provider.__aenter__.assert_awaited_once()
    provider.__aexit__.assert_awaited_once()


async def test_job_postgres_borrowed_provider_is_left_alone() -> None:
    """An external provider is neither opened nor closed by the adapter."""
    # Arrange
    provider = MagicMock(spec=PostgresProvider)
    backend = PostgresJobAdapter(provider=PostgresProvider(URL))
    backend._provider = provider

    # Act
    async with backend:
        pass

    # Assert
    provider.__aenter__.assert_not_called()
    provider.__aexit__.assert_not_called()


async def test_job_postgres_calls_with_nothing_to_do_open_nothing() -> None:
    """A claim with no free slot and an empty beat need no connection."""
    # Arrange
    backend = PostgresJobAdapter(provider=PostgresProvider(URL))

    # Act
    claimed = await backend.claim(slots={NAME: 0}, runner="r", lease=LEASE)
    replies = await backend.beat(runner="r", beats=[], lease=LEASE)

    # Assert
    assert (claimed, replies) == ([], [])


@pytest.fixture(scope="module")
def postgres_url() -> Generator[str]:
    """Start one Postgres for the module, and return its URL."""
    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        yield f"postgresql://test:test@localhost:{port}/test"


@pytest.fixture
async def provider(postgres_url: str) -> AsyncGenerator[PostgresProvider]:
    """Provide an open provider."""
    async with PostgresProvider(postgres_url) as postgres:
        yield postgres


@pytest.fixture
async def table_name(provider: PostgresProvider) -> AsyncGenerator[str]:
    """Return a table name of its own, and drop that table afterwards."""
    name = f"jobs_{secrets.token_hex(6)}"
    yield name
    await provider.client.execute(f"DROP TABLE IF EXISTS {name}")


async def _tables(provider: PostgresProvider) -> set[str]:
    """Return the tables of the current schema."""
    rows = await provider.client.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema();"
    )
    return {row[0] for row in rows}


@pytest.mark.integration
async def test_job_postgres_app_with_no_job_creates_no_table(
    postgres_url: str, provider: PostgresProvider
) -> None:
    """A Postgres `Coordination` with no job adds no job table."""
    # Arrange
    before = await _tables(provider)
    micro = Grelmicro(
        uses=[
            Coordination(PostgresProvider(postgres_url, table_names="legacy"))
        ]
    )

    # Act
    async with micro:
        pass

    # Assert
    created = await _tables(provider) - before
    assert not any("jobs" in table for table in created)
    assert "grelmicro_jobs" not in created


@pytest.mark.integration
@pytest.mark.parametrize(
    "first_call",
    ["submit", "claim", "beat", "settle", "get", "cancel", "reap", "purge"],
)
async def test_job_postgres_first_job_call_creates_the_table(
    provider: PostgresProvider, table_name: str, first_call: str
) -> None:
    """Opening the adapter creates nothing, and its first job call does."""
    async with provider.job_backend(table_name=table_name) as backend:
        # Arrange
        opened = await _tables(provider)
        calls = {
            "submit": lambda: backend.submit(**_submit_args("j")),
            "claim": lambda: backend.claim(
                slots={NAME: 1}, runner="r", lease=LEASE
            ),
            "beat": lambda: backend.beat(
                runner="r", beats=[Beat(NAME, "j")], lease=LEASE
            ),
            "settle": lambda: backend.settle(
                name=NAME,
                job_id="j",
                runner="r",
                status=JobStatus.SUCCEEDED,
                error=None,
                result=None,
                progress=None,
            ),
            "get": lambda: backend.get(name=NAME, job_id="j", owner=None),
            "cancel": lambda: backend.cancel(name=NAME, job_id="j", owner=None),
            "reap": lambda: backend.reap(limit=10),
            "purge": lambda: backend.purge(limit=10),
        }

        # Act
        await calls[first_call]()
        await backend.submit(**_submit_args("after"))

        # Assert
        assert table_name not in opened
        assert table_name in await _tables(provider)


@pytest.mark.integration
async def test_job_postgres_auto_migrate_off_creates_no_table(
    provider: PostgresProvider, table_name: str
) -> None:
    """With `auto_migrate=False`, the schema is left to the app."""
    async with provider.job_backend(
        table_name=table_name, auto_migrate=False
    ) as backend:
        # Act and assert
        with pytest.raises(asyncpg.UndefinedTableError):
            await backend.submit(**_submit_args("j"))

    # Assert
    assert table_name not in await _tables(provider)


@pytest.mark.integration
async def test_job_postgres_purge_deletes_up_to_its_limit(
    provider: PostgresProvider, table_name: str
) -> None:
    """With no expiry of its own, `purge` deletes ended jobs and counts them."""
    async with provider.job_backend(table_name=table_name) as backend:
        # Arrange
        for index in range(3):
            await backend.submit(
                **_submit_args(f"j{index}", start_as="r", keep=timedelta(0))
            )
            await backend.settle(
                name=NAME,
                job_id=f"j{index}",
                runner="r",
                status=JobStatus.SUCCEEDED,
                error=None,
                result=None,
                progress=None,
            )

        # Act
        first = await backend.purge(limit=2)
        second = await backend.purge(limit=10)
        third = await backend.purge(limit=10)

        # Assert
        left = await provider.client.fetchval(
            f"SELECT count(*) FROM {table_name};"  # noqa: S608
        )
    assert (first, second, third) == (2, 1, 0)
    assert left == 0


@pytest.mark.integration
async def test_job_postgres_end_state_drops_payload_and_context(
    provider: PostgresProvider, table_name: str
) -> None:
    """An ended job keeps neither its arguments nor its context."""
    async with provider.job_backend(table_name=table_name) as backend:
        # Arrange
        await backend.submit(**_submit_args("running", start_as="r"))
        await backend.submit(**_submit_args("queued"))

        # Act
        await backend.settle(
            name=NAME,
            job_id="running",
            runner="r",
            status=JobStatus.SUCCEEDED,
            error=None,
            result='"ok"',
            progress=None,
        )
        await backend.cancel(name=NAME, job_id="queued", owner=None)

        # Assert
        rows = await provider.client.fetch(
            f"SELECT payload, context FROM {table_name};"  # noqa: S608
        )
    assert [tuple(row) for row in rows] == [(None, None), (None, None)]


@pytest.mark.integration
async def test_job_postgres_payload_is_kept_byte_for_byte(
    provider: PostgresProvider, table_name: str
) -> None:
    """The payload is opaque text, so its spacing and key order survive."""
    # Arrange
    payload = '{"b":  1, "a": [ 2 ]}'
    async with provider.job_backend(table_name=table_name) as backend:
        await backend.submit(**_submit_args("j", payload=payload))

        # Act
        [claimed] = await backend.claim(
            slots={NAME: 1}, runner="r", lease=LEASE
        )

    # Assert
    assert claimed.payload == payload
    assert claimed.context == {"trace": "t"}


@pytest.mark.integration
async def test_job_postgres_tables_keep_their_jobs_apart(
    provider: PostgresProvider, table_name: str
) -> None:
    """Two adapters on one database with their own tables share no job."""
    other = f"{table_name}_b"
    try:
        async with (
            provider.job_backend(table_name=table_name) as first,
            provider.job_backend(table_name=other) as second,
        ):
            # Arrange
            await first.submit(**_submit_args("j"))

            # Act
            read = await second.get(name=NAME, job_id="j", owner=None)
            claimed = await second.claim(
                slots={NAME: 1}, runner="r", lease=LEASE
            )
    finally:
        await provider.client.execute(f"DROP TABLE IF EXISTS {other}")

    # Assert
    assert read is None
    assert claimed == []


@pytest.mark.integration
async def test_job_postgres_replicas_share_the_queue(
    postgres_url: str, provider: PostgresProvider, table_name: str
) -> None:
    """Replicas with their own pools never pass `max_queued` or share a job."""
    # Arrange
    replicas = [PostgresProvider(postgres_url) for _ in range(3)]
    async with (
        replicas[0] as a,
        replicas[1] as b,
        replicas[2] as c,
    ):
        backends = [
            p.job_backend(table_name=table_name) for p in (a, b, c, provider)
        ]

        # Act
        submitted = await asyncio.gather(
            *(
                backends[index % 4].submit(
                    **_submit_args(f"j{index}", max_queued=5)
                )
                for index in range(20)
            )
        )
        claimed = await asyncio.gather(
            *(
                backend.claim(slots={NAME: 2}, runner=f"r{index}", lease=LEASE)
                for index, backend in enumerate(backends)
            )
        )

    # Assert
    assert sum(s.record is not None for s in submitted) == 5  # noqa: PLR2004
    ids = [job.record.id for batch in claimed for job in batch]
    assert len(ids) == len(set(ids)) == 5  # noqa: PLR2004


@pytest.mark.integration
async def test_job_postgres_submit_racing_the_same_id_returns_the_stored_job(
    provider: PostgresProvider, table_name: str
) -> None:
    """A submit that loses the insert to another one reads back that job."""
    async with provider.job_backend(table_name=table_name) as backend:
        # Arrange
        await backend.submit(**_submit_args("warm"))
        async with provider.client.acquire() as conn:
            insert = conn.transaction()
            await insert.start()
            await conn.execute(
                f"INSERT INTO {table_name} "  # noqa: S608
                "(id, name, owner, status, version, payload, context, "
                "keep_us, created_at) VALUES ('j', $1, NULL, 'queued', 1, "
                "'{}', '{}', 3600000000, clock_timestamp());",
                NAME,
            )
            submit = asyncio.create_task(backend.submit(**_submit_args("j")))
            await asyncio.sleep(0.5)

            # Act
            await insert.commit()
            submitted = await submit

        # Assert
        read = await backend.get(name=NAME, job_id="j", owner=None)
    assert submitted.record is not None
    assert submitted.record == read
    assert submitted.started is False
    count = await provider.client.fetchval(
        f"SELECT count(*) FROM {table_name} WHERE id = 'j';"  # noqa: S608
    )
    assert count == 1


@pytest.mark.integration
async def test_job_postgres_submit_after_a_lock_wait_reads_the_clock_again(
    provider: PostgresProvider, table_name: str
) -> None:
    """A job that expired while a retried submit waited reads as gone."""
    async with provider.job_backend(table_name=table_name) as backend:
        # Arrange
        await backend.submit(**_submit_args("warm"))
        async with provider.client.acquire() as conn:
            insert = conn.transaction()
            await insert.start()
            await conn.execute(
                f"INSERT INTO {table_name} "  # noqa: S608
                "(id, name, owner, status, version, keep_us, created_at, "
                "started_at, finished_at, expires_at) VALUES ('j', $1, NULL, "
                "'succeeded', 2, 100000, clock_timestamp(), clock_timestamp(), "
                "clock_timestamp(), clock_timestamp() + interval '100 ms');",
                NAME,
            )
            submit = asyncio.create_task(backend.submit(**_submit_args("j")))
            await asyncio.sleep(0.5)

            # Act
            await insert.commit()
            submitted = await submit

    # Assert
    assert submitted.record is not None
    assert submitted.record.status is JobStatus.QUEUED


def test_job_postgres_refuses_a_table_name_that_cuts_an_index_name() -> None:
    """Postgres cuts names at 63 bytes, so a longer index name would collide."""
    # Arrange
    provider = PostgresProvider("postgresql://test:test@localhost:5432/test")

    # Act and assert
    PostgresJobAdapter(provider=provider, table_name="j" * 48)
    with pytest.raises(SettingsValidationError, match="48 characters"):
        PostgresJobAdapter(provider=provider, table_name="j" * 49)
