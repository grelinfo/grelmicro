"""Test what only the SQLite job backend promises."""

import sqlite3
from collections.abc import AsyncGenerator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.task import JobStatus
from grelmicro.task._protocol import Beat, JobBackend
from grelmicro.task.sqlite import SQLiteJobAdapter
from tests._future import DEFAULT_TABLE_NAMES

pytestmark = [pytest.mark.timeout(10)]

NAME = "app:job"
LEASE = timedelta(seconds=30)


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


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncGenerator[SQLiteProvider]:
    """Provide an open provider on a file."""
    async with SQLiteProvider(tmp_path / "jobs.db") as sqlite:
        yield sqlite


async def _tables(provider: SQLiteProvider) -> set[str]:
    """Return the tables of the provider's file."""
    rows = await provider.client.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table';"
    )
    return {row[0] for row in rows}


def test_job_sqlite_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    # Arrange
    path = str(tmp_path / "env.db")
    monkeypatch.setenv("JOBS_SQLITE_PATH", path)

    # Act
    backend = SQLiteJobAdapter(env_prefix="JOBS_SQLITE_")

    # Assert
    assert backend.provider.path == path
    assert backend.provider.env_prefix == "JOBS_SQLITE_"
    assert backend._owns_provider is True


def test_job_sqlite_adapter_borrows_an_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    # Arrange
    provider = SQLiteProvider(":memory:")

    # Act
    backend = SQLiteJobAdapter(provider=provider)

    # Assert
    assert backend.provider is provider
    assert backend._owns_provider is False


def test_job_sqlite_provider_factory_returns_the_adapter() -> None:
    """`SQLiteProvider.job_backend()` returns a bound adapter."""
    # Arrange
    provider = SQLiteProvider(":memory:")

    # Act
    backend = provider.job_backend(table_name="jobs")

    # Assert
    assert isinstance(backend, SQLiteJobAdapter)
    assert isinstance(backend, JobBackend)
    assert backend.provider is provider
    assert backend._table_name == "jobs"
    assert SQLiteJobAdapter.scope == "host"


def test_job_sqlite_rebind_provider_borrows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_rebind_provider` swaps the provider and marks it as not owned."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", ":memory:")
    backend = SQLiteJobAdapter()
    other = SQLiteProvider(":memory:")

    # Act
    backend._rebind_provider(other)

    # Assert
    assert backend.provider is other
    assert backend._owns_provider is False


@pytest.mark.parametrize("table_name", ["1jobs", "jobs; DROP", "jobs-x", ""])
def test_job_sqlite_table_name_must_be_an_identifier(table_name: str) -> None:
    """A table name that is not a SQL identifier is refused."""
    # Act and assert
    with pytest.raises(SettingsValidationError, match="not a valid SQL"):
        SQLiteJobAdapter(
            provider=SQLiteProvider(":memory:"), table_name=table_name
        )


async def test_job_sqlite_owned_provider_is_opened_and_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When owned, the adapter opens and closes its provider."""
    # Arrange
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "owned.db"))
    backend = SQLiteJobAdapter()

    # Act
    async with backend:
        submitted = await backend.submit(**_submit_args("j"))

    # Assert
    assert submitted.record is not None
    assert backend.provider._conn is None


async def test_job_sqlite_borrowed_provider_is_left_open(
    provider: SQLiteProvider,
) -> None:
    """An external provider stays open after the adapter exits."""
    # Act
    async with provider.job_backend():
        pass

    # Assert
    await provider.check()


@DEFAULT_TABLE_NAMES
async def test_job_sqlite_app_with_no_job_creates_no_table(
    tmp_path: Path,
) -> None:
    """A SQLite `Coordination` with no job leaves the file without a job table."""
    # Arrange
    path = tmp_path / "app.db"
    micro = Grelmicro(
        uses=[Coordination(SQLiteProvider(path), requires="host")]
    )

    # Act
    async with micro:
        pass

    # Assert
    with closing(sqlite3.connect(path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table';"
            )
        }
    assert "locks" in tables
    assert "grelmicro_jobs" not in tables


@pytest.mark.parametrize(
    "first_call",
    ["submit", "claim", "beat", "settle", "get", "cancel", "reap", "purge"],
)
async def test_job_sqlite_first_job_call_creates_the_table(
    provider: SQLiteProvider, first_call: str
) -> None:
    """Opening the adapter creates nothing, and its first job call does."""
    async with provider.job_backend() as backend:
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
        assert "grelmicro_jobs" not in opened
        assert "grelmicro_jobs" in await _tables(provider)


async def test_job_sqlite_auto_migrate_off_creates_no_table(
    provider: SQLiteProvider,
) -> None:
    """With `auto_migrate=False`, the schema is left to the app."""
    async with provider.job_backend(auto_migrate=False) as backend:
        # Act and assert
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            await backend.submit(**_submit_args("j"))

    # Assert
    assert "grelmicro_jobs" not in await _tables(provider)


async def test_job_sqlite_calls_with_nothing_to_do_open_nothing() -> None:
    """A claim with no free slot and an empty beat need no connection."""
    # Arrange
    backend = SQLiteJobAdapter(provider=SQLiteProvider(":memory:"))

    # Act
    claimed = await backend.claim(slots={NAME: 0}, runner="r", lease=LEASE)
    replies = await backend.beat(runner="r", beats=[], lease=LEASE)

    # Assert
    assert (claimed, replies) == ([], [])


async def test_job_sqlite_purge_deletes_up_to_its_limit(
    provider: SQLiteProvider,
) -> None:
    """With no expiry of its own, `purge` deletes ended jobs and counts them."""
    async with provider.job_backend() as backend:
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
        [[left]] = await provider.client.execute_fetchall(
            "SELECT count(*) FROM grelmicro_jobs;"
        )
    assert (first, second, third) == (2, 1, 0)
    assert left == 0


async def test_job_sqlite_end_state_drops_payload_and_context(
    provider: SQLiteProvider,
) -> None:
    """An ended job keeps neither its arguments nor its context."""
    async with provider.job_backend() as backend:
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
        rows = await provider.client.execute_fetchall(
            "SELECT payload, context FROM grelmicro_jobs;"
        )
    assert [tuple(row) for row in rows] == [(None, None), (None, None)]


async def test_job_sqlite_jobs_survive_a_reopen(tmp_path: Path) -> None:
    """A job stored in the file is there for the next provider to open it."""
    # Arrange
    path = tmp_path / "jobs.db"
    async with (
        SQLiteProvider(path) as first,
        first.job_backend() as backend,
    ):
        await backend.submit(**_submit_args("kept"))

    # Act
    async with (
        SQLiteProvider(path) as second,
        second.job_backend() as backend,
    ):
        read = await backend.get(name=NAME, job_id="kept", owner=None)

    # Assert
    assert read is not None
    assert read.status is JobStatus.QUEUED


async def test_job_sqlite_tables_keep_their_jobs_apart(
    provider: SQLiteProvider,
) -> None:
    """Two adapters on one file with their own tables share no job."""
    async with (
        provider.job_backend(table_name="jobs_a") as first,
        provider.job_backend(table_name="jobs_b") as second,
    ):
        # Arrange
        await first.submit(**_submit_args("j"))

        # Act
        read = await second.get(name=NAME, job_id="j", owner=None)
        claimed = await second.claim(slots={NAME: 1}, runner="r", lease=LEASE)

    # Assert
    assert read is None
    assert claimed == []
