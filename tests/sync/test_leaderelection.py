"""Test leader election."""

import math

import pytest
from anyio import Event, create_task_group, sleep
from pydantic import ValidationError
from pytest_mock import MockerFixture

from grelmicro.abc.lockbackend import LockBackend
from grelmicro.backends.memory import MemoryLockBackend
from grelmicro.sync.leaderelection import LeaderElection, LeaderElectionConfig

WORKERS = 4
WORKER_1 = 0
WORKER_2 = 1
TEST_TIMEOUT = 1

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(TEST_TIMEOUT)]


@pytest.fixture
def backend() -> LockBackend:
    """Lock Backend."""
    return MemoryLockBackend()


@pytest.fixture
def configs() -> list[LeaderElectionConfig]:
    """Leader election Config."""
    return [
        LeaderElectionConfig(
            name="test_leader_election",
            worker=f"worker_{i}",
            lease_duration=0.01,
            renew_deadline=0.008,
            retry_interval=0.001,
            error_interval=0.01,
            backend_timeout=0.007,
        )
        for i in range(WORKERS)
    ]


@pytest.fixture
def leader_elections(
    backend: LockBackend, configs: list[LeaderElectionConfig]
) -> list[LeaderElection]:
    """Leader elections."""
    return [
        LeaderElection(backend=backend, **configs[i].model_dump())
        for i in range(WORKERS)
    ]


@pytest.fixture
def leader_election(
    backend: LockBackend, configs: list[LeaderElectionConfig]
) -> LeaderElection:
    """Leader election."""
    return LeaderElection(backend=backend, **configs[WORKER_1].model_dump())


async def wait_first_leader(leader_elections: list[LeaderElection]) -> None:
    """Wait for the first leader to be elected."""

    async def wrapper(leader_election: LeaderElection, event: Event) -> None:
        """Wait for the leadership."""
        await leader_election.wait_for_leader()
        event.set()

    async with create_task_group() as task_group:
        event = Event()
        for coroutine in leader_elections:
            task_group.start_soon(wrapper, coroutine, event)
        await event.wait()
        task_group.cancel_scope.cancel()


def test_leader_election_config() -> None:
    """Test leader election Config."""
    # Arrange
    config = LeaderElectionConfig(
        name="test_leader_election",
        worker="worker_1",
        lease_duration=0.01,
        renew_deadline=0.008,
        retry_interval=0.001,
        error_interval=0.01,
        backend_timeout=0.007,
    )

    # Assert
    assert config.model_dump() == {
        "name": "test_leader_election",
        "worker": "worker_1",
        "lease_duration": 0.01,
        "renew_deadline": 0.008,
        "retry_interval": 0.001,
        "error_interval": 0.01,
        "backend_timeout": 0.007,
    }


def test_leader_election_config_defaults() -> None:
    """Test leader election Config Defaults."""
    # Arrange
    config = LeaderElectionConfig(name="test_leader_election", worker="worker_1")

    # Assert
    assert config.model_dump() == {
        "name": "test_leader_election",
        "worker": "worker_1",
        "lease_duration": 15,
        "renew_deadline": 10,
        "retry_interval": 2,
        "error_interval": 30,
        "backend_timeout": 5,
    }


def test_leader_election_config_validation_errors() -> None:
    """Test leader election Config Errors."""
    # Arrange
    with pytest.raises(
        ValidationError, match="Renew deadline must be shorter than lease duration."
    ):
        LeaderElectionConfig(
            name="test_leader_election",
            worker="worker_1",
            lease_duration=15,
            renew_deadline=20,
        )
    with pytest.raises(
        ValidationError, match="Retry interval must be shorter than renew deadline."
    ):
        LeaderElectionConfig(
            name="test_leader_election",
            worker="worker_1",
            renew_deadline=10,
            retry_interval=15,
        )
    with pytest.raises(
        ValidationError, match="Backend timeout must be shorter than renew deadline."
    ):
        LeaderElectionConfig(
            name="test_leader_election",
            worker="worker_1",
            renew_deadline=10,
            backend_timeout=15,
        )


async def test_lifecycle(leader_election: LeaderElection) -> None:
    """Test leader election on worker complete lifecycle."""
    # Act
    is_leader_before_start = leader_election.is_leader()
    is_running_before_start = leader_election.is_running()
    async with create_task_group() as tg:
        await tg.start(leader_election)
        is_running_after_start = leader_election.is_running()
        await leader_election.wait_for_leader()
        is_leader_after_start = leader_election.is_leader()
        tg.cancel_scope.cancel()
    is_running_after_cancel = leader_election.is_running()
    await leader_election.wait_lose_leader()
    is_leader_after_cancel = leader_election.is_leader()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_after_start is True
    assert is_leader_after_cancel is False

    assert is_running_before_start is False
    assert is_running_after_start is True
    assert is_running_after_cancel is False


async def test_leader_election_context_manager(leader_election: LeaderElection) -> None:
    """Test leader election on worker using context manager."""
    # Act
    is_leader_before_start = leader_election.is_leader()
    async with create_task_group() as tg:
        await tg.start(leader_election)
        async with leader_election:
            is_leader_inside_context = leader_election.is_leader()
        is_leader_after_context = leader_election.is_leader()
        tg.cancel_scope.cancel()
    await leader_election.wait_lose_leader()
    is_leader_after_cancel = leader_election.is_leader()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_inside_context is True
    assert is_leader_after_context is True
    assert is_leader_after_cancel is False


async def test_leader_election_single_worker(leader_election: LeaderElection) -> None:
    """Test leader election on single worker."""
    # Act
    async with create_task_group() as tg:
        is_leader_before_start = leader_election.is_leader()
        await tg.start(leader_election)
        is_leader_inside_context = leader_election.is_leader()
        tg.cancel_scope.cancel()
    await leader_election.wait_lose_leader()
    is_leader_after_cancel = leader_election.is_leader()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_inside_context is True
    assert is_leader_after_cancel is False


async def test_leadership_abandon_on_renew_deadline_reached(
    leader_election: LeaderElection,
) -> None:
    """Test leader election abandons leadership when renew deadline is reached."""
    # Act
    is_leader_before_start = leader_election.is_leader()
    async with create_task_group() as tg:
        await tg.start(leader_election)
        await leader_election.wait_for_leader()
        is_leader_after_start = leader_election.is_leader()
        leader_election.config.retry_interval = math.inf
        await leader_election.wait_lose_leader()
        is_leader_after_not_renewed = leader_election.is_leader()
        tg.cancel_scope.cancel()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_after_start is True
    assert is_leader_after_not_renewed is False


async def test_leadership_abandon_on_backend_failure(
    leader_election: LeaderElection,
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
) -> None:
    """Test leader election abandons leadership when backend is unreachable."""
    # Arrange
    caplog.set_level("WARNING")

    # Act
    is_leader_before_start = leader_election.is_leader()
    async with create_task_group() as tg:
        await tg.start(leader_election)
        await leader_election.wait_for_leader()
        is_leader_after_start = leader_election.is_leader()
        mocker.patch.object(
            leader_election.backend,
            "acquire",
            side_effect=Exception("Backend Unreachable"),
        )
        await leader_election.wait_lose_leader()
        is_leader_after_not_renewed = leader_election.is_leader()
        tg.cancel_scope.cancel()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_after_start is True
    assert is_leader_after_not_renewed is False
    assert (
        "Leader Election lost leadership: test_leader_election (renew deadline reached)"
        in caplog.messages
    )


async def test_unepexpected_stop(
    leader_election: LeaderElection, mocker: MockerFixture
) -> None:
    """Test leader election worker abandons leadership on unexpected stop."""

    # Arrange
    async def leader_election_unexpected_exception() -> None:
        async with create_task_group() as tg:
            await tg.start(leader_election)
            await leader_election.wait_for_leader()
            mock = mocker.patch.object(
                leader_election,
                "_try_acquire_or_renew",
                side_effect=Exception("Unexpected Exception"),
            )
            await leader_election.wait_lose_leader()
            mock.reset_mock()
            tg.cancel_scope.cancel()

    # Act / Assert
    with pytest.raises(ExceptionGroup):
        await leader_election_unexpected_exception()


async def test_release_on_cancel(
    backend: LockBackend, leader_election: LeaderElection, mocker: MockerFixture
) -> None:
    """Test leader election on worker that releases the lock on cancel."""
    # Arrange
    spy_release = mocker.spy(backend, "release")

    # Act
    async with create_task_group() as tg:
        await tg.start(leader_election)
        await leader_election.wait_for_leader()
        tg.cancel_scope.cancel()
    await leader_election.wait_lose_leader()

    # Assert
    spy_release.assert_called_once()


async def test_release_failure_ignored(
    backend: LockBackend,
    leader_election: LeaderElection,
    mocker: MockerFixture,
) -> None:
    """Test leader election on worker that ignores release failure."""
    # Arrange
    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Unreachable")
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(leader_election)
        await leader_election.wait_for_leader()
        tg.cancel_scope.cancel()
    await leader_election.wait_lose_leader()


async def test_only_one_leader(leader_elections: list[LeaderElection]) -> None:
    """Test leader election on multiple workers ensuring only one leader is elected."""
    # Act
    leaders_before_start = [
        leader_election.is_leader() for leader_election in leader_elections
    ]
    async with create_task_group() as tg:
        for leader_election in leader_elections:
            await tg.start(leader_election)
        await wait_first_leader(leader_elections)
        leaders_after_start = [
            leader_election.is_leader() for leader_election in leader_elections
        ]
        tg.cancel_scope.cancel()
    for leader_election in leader_elections:
        await leader_election.wait_lose_leader()
    leaders_after_cancel = [
        leader_election.is_leader() for leader_election in leader_elections
    ]

    # Assert
    assert sum(leaders_before_start) == 0
    assert sum(leaders_after_start) == 1
    assert sum(leaders_after_cancel) == 0


async def test_leader_transition(
    leader_elections: list[LeaderElection],
) -> None:
    """Test leader election leader transition to another worker."""
    # Arrange
    leaders_after_leader_election1_start = [False] * len(leader_elections)
    leaders_after_all_start = [False] * len(leader_elections)
    leaders_after_leader_election1_down = [False] * len(leader_elections)

    # Act
    leaders_before_start = [
        leader_election.is_leader() for leader_election in leader_elections
    ]
    async with create_task_group() as workers_tg:
        async with create_task_group() as worker1_tg:
            await worker1_tg.start(leader_elections[WORKER_1])
            await leader_elections[WORKER_1].wait_for_leader()
            leaders_after_leader_election1_start = [
                leader_election.is_leader() for leader_election in leader_elections
            ]

            for leader_election in leader_elections:
                await workers_tg.start(leader_election)
            leaders_after_all_start = [
                leader_election.is_leader() for leader_election in leader_elections
            ]
            worker1_tg.cancel_scope.cancel()

        await leader_elections[WORKER_1].wait_lose_leader()

        await wait_first_leader(leader_elections)
        leaders_after_leader_election1_down = [
            leader_election.is_leader() for leader_election in leader_elections
        ]
        workers_tg.cancel_scope.cancel()

    for leader_election in leader_elections[WORKER_2:]:
        await leader_election.wait_lose_leader()
    leaders_after_all_down = [
        leader_election.is_leader() for leader_election in leader_elections
    ]

    # Assert
    assert sum(leaders_before_start) == 0
    assert sum(leaders_after_leader_election1_start) == 1
    assert sum(leaders_after_all_start) == 1
    assert sum(leaders_after_leader_election1_down) == 1
    assert sum(leaders_after_all_down) == 0

    assert leaders_after_leader_election1_start[WORKER_1] is True
    assert leaders_after_leader_election1_down[WORKER_1] is False


async def test_error_interval(
    backend: LockBackend,
    leader_elections: list[LeaderElection],
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
) -> None:
    """Test leader election on worker with error cooldown."""
    # Arrange
    caplog.set_level("ERROR")
    leader_elections[WORKER_1].config.error_interval = 1
    leader_elections[WORKER_2].config.error_interval = 0.001
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Unreachable")
    )

    # Act
    async with create_task_group() as tg:
        await tg.start(leader_elections[WORKER_1])
        await sleep(0.01)
        tg.cancel_scope.cancel()
    leader_election1_nb_errors = sum(
        1 for record in caplog.records if record.levelname == "ERROR"
    )
    caplog.clear()

    async with create_task_group() as tg:
        await tg.start(leader_elections[WORKER_2])
        await sleep(0.01)
        tg.cancel_scope.cancel()
    leader_election2_nb_errors = sum(
        1 for record in caplog.records if record.levelname == "ERROR"
    )

    # Assert
    assert leader_election1_nb_errors == 1
    assert leader_election2_nb_errors >= 1
