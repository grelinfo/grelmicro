"""Test how a job finds its store, and the check that refuses a local one."""

import asyncio
import warnings
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Self, cast

import pytest

from grelmicro import (
    BackendScopeError,
    BackendScopeWarning,
    Grelmicro,
    Grelmicro046Warning,
)
from grelmicro.coordination import Coordination
from grelmicro.coordination.errors import CoordinationBackendError
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.providers import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.task import JobStatus, Progress, Tasks
from grelmicro.task.memory import MemoryJobAdapter
from grelmicro.types import BackendScope, Environment
from tests._future import DEFAULT_TABLE_NAMES

pytestmark = [pytest.mark.timeout(10)]

STRICT_ENVIRONMENTS: list[Environment] = ["staging", "production"]
QUIET_ENVIRONMENTS: list[Environment] = ["development", "test"]


class _NoJobProvider(Provider):
    """A provider that ships no backend, so no job store either."""

    short_name = "nojob"

    async def __aenter__(self) -> Self:
        """Open nothing."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close nothing."""


class _SharedJobStore(MemoryJobAdapter):
    """A memory store that says it is shared, as Redis is."""

    scope: ClassVar[BackendScope] = "cluster"


class _Client(MemoryJobAdapter):
    """One replica's client to a store every replica shares."""

    def __init__(self, store: MemoryJobAdapter) -> None:
        vars(self).update(vars(store))


@pytest.fixture
def _undeclared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare no tier at all."""
    monkeypatch.delenv("GREL_ENVIRONMENT", raising=False)


async def _noop(_progress: Progress) -> None:
    """Do nothing."""


async def _forever(_progress: Progress) -> None:
    """Run until cancelled."""
    await asyncio.Event().wait()


async def _settled() -> None:
    """Let every ready task run."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.parametrize("environment", STRICT_ENVIRONMENTS)
async def test_job_without_a_shared_store_is_refused_in_a_strict_environment(
    environment: Environment,
) -> None:
    """A poll reaching another replica would answer `404`, so it never starts."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report")(_noop)
    micro = Grelmicro(uses=[tasks], environment=environment)

    # Act
    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    # Assert
    message = str(error.value)
    assert "Job('report')" in message
    assert "provides scope 'process', but requires scope 'cluster'" in message
    assert "a Redis, Valkey, or Postgres provider" in message
    assert "Kubernetes" not in message
    assert "pass requires=" in message


@pytest.mark.usefixtures("_undeclared")
async def test_job_without_a_shared_store_warns_once_when_no_tier_is_set() -> (
    None
):
    """With `GREL_ENVIRONMENT` unset, jobs live in the process and it warns."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report")(_noop)
    micro = Grelmicro(uses=[tasks])

    # Act
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with micro:
            pass

    # Assert
    reports = [w for w in caught if issubclass(w.category, BackendScopeWarning)]
    assert len(reports) == 1
    assert "Job('report')" in str(reports[0].message)
    assert "requires='process'" in str(reports[0].message)


@pytest.mark.parametrize("environment", QUIET_ENVIRONMENTS)
async def test_job_without_a_shared_store_is_quiet_in_a_local_environment(
    environment: Environment,
) -> None:
    """In `development` and `test`, jobs live in the process with no report."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(name="report")(_noop)
    micro = Grelmicro(uses=[tasks], environment=environment)

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with micro:
            submitted = await job.without_owner().submit()

    # Assert
    assert submitted.name == "report"


@pytest.mark.parametrize(
    "environment", [*STRICT_ENVIRONMENTS, *QUIET_ENVIRONMENTS, None]
)
async def test_job_requiring_only_the_process_opens_everywhere(
    environment: Environment | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`requires="process"` declares one replica on purpose."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT", raising=False)
    tasks = Tasks()
    tasks.job(name="report", requires="process")(_noop)
    micro = Grelmicro(uses=[tasks], environment=environment)

    # Act and assert
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with micro:
            pass


async def test_job_requiring_the_host_is_refused_on_a_process_store() -> None:
    """`requires=` names the reach checked, not only an opt-out."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report", requires="host")(_noop)
    micro = Grelmicro(uses=[tasks], environment="production")

    # Act and assert
    with pytest.raises(BackendScopeError, match="requires scope 'host'"):
        await micro.__aenter__()


async def test_job_requiring_the_host_offers_sqlite() -> None:
    """SQLite reaches far enough for `host`, so the fix names it."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report", requires="host")(_noop)
    micro = Grelmicro(uses=[tasks], environment="production")

    # Act
    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    # Assert
    message = str(error.value)
    assert "a SQLite, Redis, Valkey, or Postgres provider" in message
    assert "Kubernetes" not in message


@pytest.mark.parametrize("environment", STRICT_ENVIRONMENTS)
@DEFAULT_TABLE_NAMES
async def test_job_on_sqlite_is_refused_unless_it_requires_the_host(
    environment: Environment, tmp_path: Path
) -> None:
    """SQLite is shared by one host only, so a job must say that is enough."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report")(_noop)
    micro = Grelmicro(
        uses=[SQLiteProvider(tmp_path / "jobs.db"), tasks],
        environment=environment,
    )

    # Act
    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    # Assert
    message = str(error.value)
    assert "provides scope 'host', but requires scope 'cluster'" in message
    assert "a Redis, Valkey, or Postgres backend" in message


@pytest.mark.parametrize("environment", STRICT_ENVIRONMENTS)
@DEFAULT_TABLE_NAMES
async def test_job_on_sqlite_requiring_the_host_runs_in_a_strict_environment(
    environment: Environment, tmp_path: Path
) -> None:
    """With `requires="host"`, a job on SQLite starts and runs to its end."""
    # Arrange
    sqlite = SQLiteProvider(tmp_path / "jobs.db")
    tasks = Tasks()
    job = tasks.job(name="report", requires="host")(_noop)
    micro = Grelmicro(
        uses=[Coordination(sqlite, requires="host"), tasks],
        environment=environment,
    )

    async with micro:
        # Act
        submitted = await job.without_owner().submit()
        for _ in range(100):
            read = await sqlite.job_backend().get(
                name="report", job_id=submitted.id, owner=None
            )
            if read is not None and read.status is JobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)

    # Assert
    assert read is not None
    assert read.status is JobStatus.SUCCEEDED


def test_job_requirement_naming_no_scope_is_passed_over() -> None:
    """A typo in `requires=` warns, and does not crash startup until 0.46.0."""
    # Arrange
    tasks = Tasks()
    with pytest.warns(Grelmicro046Warning, match="'clustr'") as caught:
        tasks.job(name="report", requires=cast("Any", "clustr"))(_noop)
    assert [warning.filename for warning in caught] == [__file__]

    # Act and assert
    Grelmicro(uses=[tasks]).check_backends()


@pytest.mark.parametrize("environment", STRICT_ENVIRONMENTS)
async def test_job_on_the_apps_shared_store_opens_in_a_strict_environment(
    environment: Environment,
) -> None:
    """A shared store in the app's `Coordination` is all a job needs."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report")(_noop)
    micro = Grelmicro(
        uses=[Coordination(job=_SharedJobStore()), tasks],
        environment=environment,
    )

    # Act and assert
    async with micro:
        pass


async def test_job_on_its_own_shared_store_opens_in_production() -> None:
    """`backend=` is checked on its own, with no `Coordination` at all."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report", backend=_SharedJobStore())(_noop)
    micro = Grelmicro(uses=[tasks], environment="production")

    # Act and assert
    async with micro:
        pass


async def test_job_on_its_own_local_store_is_refused_in_production() -> None:
    """An explicit memory store falls short like a missing one."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report", backend=MemoryJobAdapter())(_noop)
    micro = Grelmicro(uses=[tasks], environment="production")

    # Act and assert
    with pytest.raises(BackendScopeError, match=r"Job\('report'\)"):
        await micro.__aenter__()


async def test_job_on_a_coordination_with_no_job_store_is_refused() -> None:
    """A `Coordination` holding only locks leaves the job in the process."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report")(_noop)
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter(), requires="process"),
            tasks,
        ],
        environment="production",
    )

    # Act and assert
    with pytest.raises(BackendScopeError, match=r"Job\('report'\)"):
        await micro.__aenter__()


async def test_job_submitted_on_one_replica_is_read_and_cancelled_on_another() -> (
    None
):
    """Two apps on one store share their jobs through `Coordination`."""
    # Arrange
    store = MemoryJobAdapter()
    tasks_a, tasks_b = Tasks(shutdown_timeout=0), Tasks(shutdown_timeout=0)
    job_a = tasks_a.job(name="report", max_concurrent=1)(_forever)
    job_b = tasks_b.job(name="report", max_concurrent=1)(_forever)
    replica_a = Grelmicro(uses=[Coordination(job=_Client(store)), tasks_a])
    replica_b = Grelmicro(uses=[Coordination(job=_Client(store)), tasks_b])

    async with replica_a, replica_b:
        submitted = await job_a.without_owner().submit()
        await _settled()

        # Act
        read = await job_b.without_owner().get(submitted.id)
        cancelled = await job_b.without_owner().cancel(submitted.id)

    # Assert
    assert read is not None
    assert read.status is JobStatus.RUNNING
    assert cancelled is not None
    assert cancelled.cancel_requested


async def test_job_backend_wins_over_the_apps_store() -> None:
    """`backend=` keeps a job off the store the app's `Coordination` holds."""
    # Arrange
    own, apps = MemoryJobAdapter(), MemoryJobAdapter()
    tasks = Tasks()
    job = tasks.job(name="report", backend=own)(_noop)
    micro = Grelmicro(uses=[Coordination(job=apps), tasks])

    async with micro:
        # Act
        submitted = await job.without_owner().submit()

        # Assert
        found = await own.get(name="report", job_id=submitted.id, owner=None)
        missing = await apps.get(name="report", job_id=submitted.id, owner=None)
    assert found is not None
    assert missing is None


async def test_job_finds_the_store_of_a_provider_in_uses() -> None:
    """A provider in `uses=` wires its job store, with no `Coordination`."""
    # Arrange
    memory = MemoryProvider()
    tasks = Tasks()
    job = tasks.job(name="report")(_noop)
    micro = Grelmicro(uses=[memory, tasks])

    async with micro:
        # Act
        submitted = await job.without_owner().submit()

        # Assert
        found = await memory.job_backend().get(
            name="report", job_id=submitted.id, owner=None
        )
    assert found is not None


def test_coordination_takes_a_job_store() -> None:
    """A provider, a bare store or `job=` fills the job slot."""
    # Arrange
    memory = MemoryProvider()
    store = MemoryJobAdapter()

    # Act
    from_provider = Coordination(memory)
    from_store = Coordination(store)
    from_keyword = Coordination(lock=MemoryLockAdapter(), job=memory)

    # Assert
    assert from_provider.job_backend is memory.job_backend()
    assert from_store.job_backend is store
    assert from_keyword.job_backend is memory.job_backend()


def test_coordination_on_a_provider_with_no_job_store_has_none() -> None:
    """A provider that ships no job store leaves the slot empty, no error."""
    # Arrange
    coordination = Coordination(_NoJobProvider())

    # Act and assert
    with pytest.raises(CoordinationBackendError, match="no job backend"):
        _ = coordination.job_backend


def test_provider_without_a_job_store_says_so() -> None:
    """The base factory raises, as every other factory does."""
    # Arrange
    provider = _NoJobProvider()

    # Act and assert
    with pytest.raises(NotImplementedError, match="has no job adapter"):
        provider.job_backend()


async def test_job_on_a_coordination_with_no_job_store_runs_in_the_process() -> (
    None
):
    """In `test`, a `Coordination` holding only locks leaves jobs local."""
    # Arrange
    tasks = Tasks()
    job = tasks.job(name="report")(_noop)
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter()), tasks])

    async with micro:
        # Act
        submitted = await job.without_owner().submit()
        await _settled()

        # Assert
        read = await job.without_owner().get(submitted.id)
    assert read is not None
    assert read.status is JobStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("requires", "expected"),
    [(None, "cluster"), ("process", "process"), ("Host", "host")],
)
def test_job_requires_reads_the_declared_reach(
    requires: BackendScope | None, expected: BackendScope
) -> None:
    """`requires` defaults to `"cluster"`, and matches a scope in any case."""
    # Arrange
    tasks = Tasks()

    # Act
    job = tasks.job(requires=requires)(_noop)

    # Assert
    assert job.requires == expected


async def test_job_and_its_coordination_falling_short_offer_only_a_job_store() -> (
    None
):
    """With a job among the findings, the fix names a store that holds jobs."""
    # Arrange
    tasks = Tasks()
    tasks.job(name="report")(_noop)
    micro = Grelmicro(
        uses=[Coordination(MemoryProvider()), tasks], environment="production"
    )

    # Act
    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    # Assert
    message = str(error.value)
    assert "Job('report')" in message
    assert "a Redis, Valkey, or Postgres backend" in message
    assert "Kubernetes" not in message
