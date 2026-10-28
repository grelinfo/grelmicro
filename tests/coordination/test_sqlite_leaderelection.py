"""Tests for the SQLite leader election adapter."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_mock

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro.coordination._protocol import LeaderElectionBackend
from grelmicro.coordination.leaderelection import LeaderElection
from grelmicro.coordination.sqlite import SQLiteLeaderElectionAdapter
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.task import Tasks
from tests.task._helpers import cancel_group, start_task

pytestmark = [pytest.mark.timeout(15)]

LEASE = timedelta(milliseconds=200)
"""A lease short enough to expire inside a test."""

EXPIRED = LEASE.total_seconds() + 0.1
"""Seconds to wait for `LEASE` to run out."""

ELECTION = "inverter"
"""The election every `LeaderElection` here stands in."""

ELECTION_KEY = f"leader:{ELECTION}"
"""The name a `LeaderElection` stores its lease under on the backend."""

POLLED = asyncio.Event()
"""Set by `poll`, the leader-gated task."""


async def poll() -> None:
    """Record that the leader-gated task ran."""
    POLLED.set()


@pytest.fixture
async def backend(
    tmp_path: Path,
) -> AsyncGenerator[SQLiteLeaderElectionAdapter]:
    """Open a SQLite leader election adapter on a temp file."""
    provider = SQLiteProvider(tmp_path / "leader.db")
    async with (
        provider,
        SQLiteLeaderElectionAdapter(provider=provider) as adapter,
    ):
        yield adapter


def _election(
    backend: SQLiteLeaderElectionAdapter, worker: str
) -> LeaderElection:
    """Return an election on `backend` that renews often on a short lease."""
    return LeaderElection(
        ELECTION,
        backend=backend,
        worker=worker,
        lease_duration=LEASE,
        renew_deadline=timedelta(milliseconds=150),
        retry_interval=0.02,
        error_interval=0.02,
        backend_timeout=0.1,
    )


# Construction and wiring.


@pytest.mark.parametrize(
    "table_name",
    [
        "leader table",
        "%leader",
        "leader;table",
        "leader' OR '1'='1",
        "leader; DROP TABLE users; --",
    ],
)
def test_table_name_invalid(table_name: str) -> None:
    """Invalid SQL identifiers for the table name raise."""
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        SQLiteLeaderElectionAdapter(
            provider=SQLiteProvider(":memory:"), table_name=table_name
        )


async def test_out_of_context_errors() -> None:
    """Adapter methods raise when called outside the context manager."""
    adapter = SQLiteLeaderElectionAdapter(provider=SQLiteProvider(":memory:"))

    with pytest.raises(OutOfContextError):
        await adapter.acquire_or_renew(name="e", token="t", duration=LEASE)
    with pytest.raises(OutOfContextError):
        await adapter.release(name="e", token="t")
    with pytest.raises(OutOfContextError):
        await adapter.get(name="e")


def test_no_provider_builds_implicit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds one from `SQLITE_PATH`."""
    monkeypatch.setenv("SQLITE_PATH", "leader.db")

    adapter = SQLiteLeaderElectionAdapter()

    assert adapter.provider.path == "leader.db"
    assert adapter._owns_provider is True


def test_explicit_provider_is_borrowed() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = SQLiteProvider("leader.db")

    adapter = SQLiteLeaderElectionAdapter(provider=provider)

    assert adapter.provider is provider
    assert adapter._owns_provider is False


def test_rebind_provider_borrows() -> None:
    """`_rebind_provider` swaps the provider and marks it borrowed."""
    adapter = SQLiteLeaderElectionAdapter(provider=SQLiteProvider("a.db"))
    adapter._owns_provider = True
    shared = SQLiteProvider("shared.db")

    adapter._rebind_provider(shared)

    assert adapter.provider is shared
    assert adapter._owns_provider is False


def test_sqlite_env_var_settings_validation_error() -> None:
    """A missing path raises a settings validation error."""
    with pytest.raises(SettingsValidationError, match="SQLITE_PATH"):
        SQLiteLeaderElectionAdapter()


def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    adapter = SQLiteLeaderElectionAdapter(
        provider=SQLiteProvider(":memory:"), table_name="my_leaders"
    )

    assert adapter._table_name == "my_leaders"


def test_provider_factory_returns_bound_adapter() -> None:
    """`provider.leaderelection_backend()` binds an adapter to the provider."""
    provider = SQLiteProvider(":memory:")

    adapter = provider.leaderelection_backend(table_name="elections")

    assert isinstance(adapter, SQLiteLeaderElectionAdapter)
    assert adapter.provider is provider
    assert adapter._owns_provider is False
    assert adapter._table_name == "elections"


def test_satisfies_protocol() -> None:
    """The adapter satisfies the `LeaderElectionBackend` protocol."""
    adapter = SQLiteLeaderElectionAdapter(provider=SQLiteProvider(":memory:"))

    assert isinstance(adapter, LeaderElectionBackend)


def test_scope_is_host() -> None:
    """State is shared by the processes that open the same file."""
    assert SQLiteLeaderElectionAdapter.scope == "host"


def test_coordination_on_sqlite_provider_serves_leader_election() -> None:
    """`Coordination(sqlite)` builds a SQLite leader election backend."""
    coordination = Coordination(SQLiteProvider(":memory:"))

    assert isinstance(
        coordination.leaderelection_backend, SQLiteLeaderElectionAdapter
    )


async def test_owned_provider_lifecycle(
    mocker: pytest_mock.MockerFixture, tmp_path: Path
) -> None:
    """An owned provider is entered and exited with the adapter."""
    provider = SQLiteProvider(tmp_path / "leader.db")
    async with provider:
        aenter = mocker.patch.object(
            provider, "__aenter__", AsyncMock(return_value=provider)
        )
        aexit = mocker.patch.object(provider, "__aexit__", AsyncMock())
        adapter = SQLiteLeaderElectionAdapter(provider=provider)
        adapter._owns_provider = True

        async with adapter:
            aenter.assert_awaited_once()
            aexit.assert_not_awaited()

        aexit.assert_awaited_once()


async def test_borrowed_provider_left_alone(
    mocker: pytest_mock.MockerFixture, tmp_path: Path
) -> None:
    """A borrowed provider is never entered or exited by the adapter."""
    provider = SQLiteProvider(tmp_path / "leader.db")
    async with provider:
        aenter = mocker.patch.object(provider, "__aenter__", AsyncMock())
        aexit = mocker.patch.object(provider, "__aexit__", AsyncMock())

        async with SQLiteLeaderElectionAdapter(provider=provider):
            pass

        aenter.assert_not_awaited()
        aexit.assert_not_awaited()


# Lease semantics.


async def test_acquire(backend: SQLiteLeaderElectionAdapter) -> None:
    """A fresh election is acquired with transitions at zero."""
    record = await backend.acquire_or_renew(name="e", token="a", duration=LEASE)

    assert record.holder == "a"
    assert record.transitions == 0
    assert record.acquired_at == record.renewed_at
    assert record.acquired_at.tzinfo is UTC
    assert record.lease_duration == LEASE
    assert dict(record.metadata) == {}


async def test_renew_keeps_acquired_at_and_transitions(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """Renewing the same holder moves renewed_at only."""
    first = await backend.acquire_or_renew(name="e", token="a", duration=LEASE)
    await asyncio.sleep(0.01)
    second = await backend.acquire_or_renew(name="e", token="a", duration=LEASE)

    assert second.holder == "a"
    assert second.transitions == first.transitions == 0
    assert second.acquired_at == first.acquired_at
    assert second.renewed_at > first.renewed_at


async def test_live_lease_not_taken(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """A live lease cannot be taken, the caller reads the current leader."""
    first = await backend.acquire_or_renew(
        name="e", token="a", duration=LEASE, metadata={"pod": "a"}
    )

    second = await backend.acquire_or_renew(
        name="e", token="b", duration=LEASE, metadata={"pod": "b"}
    )

    assert second == first


async def test_takeover_after_expiry_bumps_transitions(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """A new holder takes an expired lease and bumps transitions."""
    first = await backend.acquire_or_renew(name="e", token="a", duration=LEASE)
    await asyncio.sleep(EXPIRED)

    record = await backend.acquire_or_renew(name="e", token="b", duration=LEASE)

    assert record.holder == "b"
    assert record.transitions == 1
    assert record.acquired_at > first.acquired_at
    assert record.acquired_at == record.renewed_at


async def test_same_holder_after_expiry_keeps_transitions(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """The same holder taking its expired lease back starts a new term."""
    first = await backend.acquire_or_renew(name="e", token="a", duration=LEASE)
    await asyncio.sleep(EXPIRED)

    record = await backend.acquire_or_renew(name="e", token="a", duration=LEASE)

    assert record.holder == "a"
    assert record.transitions == 0
    assert record.acquired_at > first.acquired_at


async def test_release(backend: SQLiteLeaderElectionAdapter) -> None:
    """Release returns True for the holder, False for anyone else."""
    await backend.acquire_or_renew(name="e", token="a", duration=LEASE)

    assert await backend.release(name="e", token="b") is False
    assert await backend.release(name="e", token="a") is True
    assert await backend.release(name="e", token="a") is False
    assert await backend.get(name="e") is None


async def test_release_of_an_expired_lease_is_refused(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """An expired lease is no longer held, so its holder cannot release it."""
    await backend.acquire_or_renew(name="e", token="a", duration=LEASE)
    await asyncio.sleep(EXPIRED)

    assert await backend.release(name="e", token="a") is False


async def test_acquire_after_release_starts_over(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """A released election is vacant, the next holder starts at zero."""
    await backend.acquire_or_renew(name="e", token="a", duration=LEASE)
    await backend.release(name="e", token="a")

    record = await backend.acquire_or_renew(name="e", token="b", duration=LEASE)

    assert record.holder == "b"
    assert record.transitions == 0


async def test_get_live_and_expired(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """Get returns the live record, then None once it expires."""
    assert await backend.get(name="e") is None
    acquired = await backend.acquire_or_renew(
        name="e", token="a", duration=LEASE
    )

    assert await backend.get(name="e") == acquired
    await asyncio.sleep(EXPIRED)
    assert await backend.get(name="e") is None


async def test_metadata_roundtrip_and_replace(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """Metadata round-trips, and a renew replaces it."""
    record = await backend.acquire_or_renew(
        name="e", token="a", duration=LEASE, metadata={"pod": "worker-1"}
    )
    renewed = await backend.acquire_or_renew(
        name="e", token="a", duration=LEASE, metadata={"version": "2"}
    )
    fetched = await backend.get(name="e")

    assert dict(record.metadata) == {"pod": "worker-1"}
    assert dict(renewed.metadata) == {"version": "2"}
    assert fetched is not None
    assert dict(fetched.metadata) == {"version": "2"}


async def test_lease_is_rounded_up_to_the_millisecond(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """A sub-millisecond lease reads back rounded up, never down."""
    record = await backend.acquire_or_renew(
        name="e", token="a", duration=timedelta(microseconds=1_000_001)
    )

    assert record.lease_duration == timedelta(milliseconds=1001)


async def test_elections_are_independent(
    backend: SQLiteLeaderElectionAdapter,
) -> None:
    """Two election names never share a lease."""
    first = await backend.acquire_or_renew(name="a", token="x", duration=LEASE)
    second = await backend.acquire_or_renew(name="b", token="y", duration=LEASE)

    assert first.holder == "x"
    assert second.holder == "y"


async def test_acquire_rolls_back_on_error(tmp_path: Path) -> None:
    """A failing acquire rolls the transaction back and re-raises."""
    provider = SQLiteProvider(tmp_path / "leader.db")
    async with (
        provider,
        SQLiteLeaderElectionAdapter(provider=provider) as adapter,
    ):
        conn = provider.client
        await conn.execute("DROP TABLE grelmicro_leader_election;")

        with pytest.raises(Exception, match="no such table"):
            await adapter.acquire_or_renew(name="e", token="a", duration=LEASE)

        assert conn.in_transaction is False


async def test_writes_leave_the_file_unlocked(tmp_path: Path) -> None:
    """After each call another connection can write the same file at once."""
    path = tmp_path / "leader.db"
    provider = SQLiteProvider(path)
    async with (
        provider,
        SQLiteLeaderElectionAdapter(provider=provider) as adapter,
    ):
        await adapter.acquire_or_renew(name="e", token="a", duration=LEASE)
        await adapter.acquire_or_renew(name="e", token="b", duration=LEASE)
        await adapter.get(name="e")
        await adapter.release(name="e", token="a")

        assert provider.client.in_transaction is False
        async with aiosqlite.connect(path, timeout=0) as other:
            await other.execute("BEGIN IMMEDIATE;")
            await other.execute("ROLLBACK;")


# Elections across providers on one file.


async def test_two_providers_on_one_file_elect_one_leader(
    tmp_path: Path,
) -> None:
    """Two providers on the same file elect exactly one leader."""
    path = tmp_path / "leader.db"
    first = SQLiteProvider(path)
    second = SQLiteProvider(path)
    async with (
        first,
        second,
        SQLiteLeaderElectionAdapter(provider=first) as backend_a,
        SQLiteLeaderElectionAdapter(provider=second) as backend_b,
    ):
        elections = [_election(backend_a, "a"), _election(backend_b, "b")]
        async with asyncio.TaskGroup() as group:
            for election in elections:
                await start_task(group, election)
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                leaders = [
                    election.is_leader_confirmed_within(0.15)
                    for election in elections
                ]
                if any(leaders) or asyncio.get_running_loop().time() > deadline:
                    break
                await asyncio.sleep(0.02)
            cancel_group(group)

    assert sum(leaders) == 1


async def test_failover_after_the_leader_stops_renewing(
    tmp_path: Path,
) -> None:
    """A leader that stops renewing is replaced once its lease runs out."""
    path = tmp_path / "leader.db"
    first = SQLiteProvider(path)
    second = SQLiteProvider(path)
    async with (
        first,
        second,
        SQLiteLeaderElectionAdapter(provider=first) as stalled,
        SQLiteLeaderElectionAdapter(provider=second) as backend,
    ):
        await stalled.acquire_or_renew(
            name=ELECTION_KEY, token="stalled", duration=LEASE
        )
        election = _election(backend, "standby")
        async with asyncio.TaskGroup() as group:
            await start_task(group, election)
            await asyncio.sleep(0.05)
            led_while_held = election.is_leader()
            await election.wait_for_leader()
            record = election.record
            cancel_group(group)

    assert led_while_held is False
    assert record is not None
    assert record.holder == "standby"
    assert record.transitions == 1


async def test_leader_gated_task_runs_on_sqlite(tmp_path: Path) -> None:
    """The leader-gated task from a bare `SQLiteProvider` app runs."""
    POLLED.clear()
    tasks = Tasks()
    micro = Grelmicro(uses=[SQLiteProvider(tmp_path / "app.db"), tasks])
    leader = LeaderElection(ELECTION)
    tasks.add_task(leader)
    tasks.every(interval=timedelta(milliseconds=20), gate=leader)(poll)

    async with micro:
        await asyncio.wait_for(POLLED.wait(), timeout=5)
        backend = micro.coordination.leaderelection_backend
        record = await backend.get(name=ELECTION_KEY)

    assert isinstance(backend, SQLiteLeaderElectionAdapter)
    assert record is not None
    assert record.holder == str(leader.config.worker)
