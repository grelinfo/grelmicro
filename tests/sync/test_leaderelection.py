"""Test leader election."""

import asyncio
import math
from asyncio import Event, sleep

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from grelmicro.errors import WouldBlockError as WouldBlock
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.leaderelection import LeaderElection, LeaderElectionConfig
from grelmicro.sync.memory import MemorySyncAdapter
from tests.task._helpers import cancel_group, start_task

LEADER_NAME = "test_leader_election"
BACKEND_LOCK_NAME = f"leader:{LEADER_NAME}"
WORKERS = 4
WORKER_1 = 0
WORKER_2 = 1
TEST_TIMEOUT = 1

pytestmark = [pytest.mark.timeout(TEST_TIMEOUT)]


@pytest.fixture
def backend() -> SyncBackend:
    """Return Memory Synchronization Backend."""
    return MemorySyncAdapter()


@pytest.fixture
def configs() -> list[LeaderElectionConfig]:
    """Leader election Config."""
    return [
        LeaderElectionConfig(
            worker=f"worker_{i}",
            lease_duration=0.02,
            renew_deadline=0.015,
            retry_interval=0.005,
            error_interval=0.01,
            backend_timeout=0.005,
        )
        for i in range(WORKERS)
    ]


@pytest.fixture
def leader_elections(
    backend: SyncBackend, configs: list[LeaderElectionConfig]
) -> list[LeaderElection]:
    """Leader elections."""
    return [
        LeaderElection.from_config(LEADER_NAME, configs[i], backend=backend)
        for i in range(WORKERS)
    ]


@pytest.fixture
def leader_election(
    backend: SyncBackend, configs: list[LeaderElectionConfig]
) -> LeaderElection:
    """Leader election."""
    return LeaderElection.from_config(
        LEADER_NAME, configs[WORKER_1], backend=backend
    )


async def wait_first_leader(leader_elections: list[LeaderElection]) -> None:
    """Wait for the first leader to be elected."""

    async def wrapper(leader_election: LeaderElection, event: Event) -> None:
        """Wait for the leadership."""
        await leader_election.wait_for_leader()
        event.set()

    async with asyncio.TaskGroup() as task_group:
        event = Event()
        for coroutine in leader_elections:
            task_group.create_task(wrapper(coroutine, event))
        await event.wait()
        cancel_group(task_group)


def test_leader_election_config() -> None:
    """Test leader election Config."""
    # Arrange
    config = LeaderElectionConfig(
        worker="worker_1",
        lease_duration=0.01,
        renew_deadline=0.008,
        retry_interval=0.001,
        error_interval=0.01,
        backend_timeout=0.007,
    )

    # Assert
    assert config.model_dump() == {
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
    config = LeaderElectionConfig(worker="worker_1")

    # Assert
    assert config.model_dump() == {
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
        ValidationError,
        match="Renew deadline must be shorter than lease duration",
    ):
        LeaderElectionConfig(
            worker="worker_1",
            lease_duration=15,
            renew_deadline=20,
        )
    with pytest.raises(
        ValidationError,
        match="Retry interval must be shorter than renew deadline",
    ):
        LeaderElectionConfig(
            worker="worker_1",
            renew_deadline=10,
            retry_interval=15,
        )
    with pytest.raises(
        ValidationError,
        match="Backend timeout must be shorter than renew deadline",
    ):
        LeaderElectionConfig(
            worker="worker_1",
            renew_deadline=10,
            backend_timeout=15,
        )


async def test_leader_key_prefix(
    backend: SyncBackend, leader_election: LeaderElection
) -> None:
    """Test LeaderElection uses prefixed key on the backend."""
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()

        # Assert - backend key should be prefixed
        assert await backend.locked(name=BACKEND_LOCK_NAME) is True
        # Raw name should NOT be locked
        assert await backend.locked(name=LEADER_NAME) is False
        cancel_group(tg)


async def test_last_confirmation_age_before_start(
    leader_election: LeaderElection,
) -> None:
    """`last_confirmation_age()` is `None` until the first acquisition."""
    assert leader_election.last_confirmation_age() is None
    assert leader_election.is_leader_confirmed_within(1.0) is False


async def test_last_confirmation_age_after_start(
    leader_election: LeaderElection,
) -> None:
    """`last_confirmation_age()` is small and `is_leader_confirmed_within` holds."""
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()

        age = leader_election.last_confirmation_age()
        assert age is not None
        assert age >= 0
        # The lease duration is 0.02s, so a 1s window must hold.
        assert leader_election.is_leader_confirmed_within(1.0) is True
        # A negative or sub-zero window cannot be satisfied.
        assert leader_election.is_leader_confirmed_within(-1.0) is False
        cancel_group(tg)


async def test_is_leader_confirmed_within_rejects_stale_age(
    leader_election: LeaderElection,
) -> None:
    """A `max_age` smaller than the confirmation gap fails the check."""
    # Drive state directly to avoid racing with the renew loop.
    await leader_election._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )
    assert leader_election.is_leader_confirmed_within(10.0) is True
    await sleep(0.05)
    assert leader_election.is_leader_confirmed_within(0.001) is False


async def test_last_confirmation_age_resets_on_confirmed_loss(
    leader_election: LeaderElection,
) -> None:
    """When the backend says `is_leader=False`, the age resets to `None`."""
    await leader_election._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )
    assert leader_election.last_confirmation_age() is not None
    await leader_election._update_state(
        is_leader=False, reason_if_no_more_leader="lock not acquired"
    )
    assert leader_election.last_confirmation_age() is None
    assert leader_election.is_leader_confirmed_within(10.0) is False


async def test_lifecycle(leader_election: LeaderElection) -> None:
    """Test leader election on worker complete lifecycle."""
    # Act
    is_leader_before_start = leader_election.is_leader()
    is_running_before_start = leader_election.is_running()
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        is_running_after_start = leader_election.is_running()
        await leader_election.wait_for_leader()
        is_leader_after_start = leader_election.is_leader()
        cancel_group(tg)
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


async def test_leader_election_context_manager(
    leader_election: LeaderElection,
) -> None:
    """Test leader election on worker using context manager."""
    # Act
    is_leader_before_start = leader_election.is_leader()
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        async with leader_election:
            is_leader_inside_context = leader_election.is_leader()
        is_leader_after_context = leader_election.is_leader()
        cancel_group(tg)
    await leader_election.wait_lose_leader()
    is_leader_after_cancel = leader_election.is_leader()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_inside_context is True
    assert is_leader_after_context is True
    assert is_leader_after_cancel is False


async def test_leader_election_single_worker(
    leader_election: LeaderElection,
) -> None:
    """Test leader election on single worker."""
    # Act
    async with asyncio.TaskGroup() as tg:
        is_leader_before_start = leader_election.is_leader()
        await start_task(tg, leader_election)
        is_leader_inside_context = leader_election.is_leader()
        cancel_group(tg)
    await leader_election.wait_lose_leader()
    is_leader_after_cancel = leader_election.is_leader()

    # Assert
    assert is_leader_before_start is False
    assert is_leader_inside_context is True
    assert is_leader_after_cancel is False


async def test_leadership_abandon_on_renew_deadline_reached(
    leader_election: LeaderElection,
    mocker: MockerFixture,
) -> None:
    """Test leader election abandons leadership when renew deadline is reached."""
    # Arrange: stop the renew loop after leadership is acquired so the
    # renew deadline elapses without state updates.
    is_leader_before_start = leader_election.is_leader()

    async def _block_forever(_: LeaderElectionConfig) -> None:
        await sleep(math.inf)

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()
        is_leader_after_start = leader_election.is_leader()
        mocker.patch.object(
            leader_election,
            "_try_acquire_or_renew",
            side_effect=_block_forever,
        )
        await leader_election.wait_lose_leader()
        is_leader_after_not_renewed = leader_election.is_leader()
        cancel_group(tg)

    # Assert
    assert is_leader_before_start is False
    assert is_leader_after_start is True
    assert is_leader_after_not_renewed is False


async def test_leadership_abandon_on_backend_error(
    leader_election: LeaderElection,
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
) -> None:
    """Test leader election abandons leadership when backend is unreachable."""
    # Arrange
    caplog.set_level("WARNING")

    # Act
    is_leader_before_start = leader_election.is_leader()
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()
        is_leader_after_start = leader_election.is_leader()
        mocker.patch.object(
            leader_election.backend,
            "acquire",
            side_effect=Exception("Backend Unreachable"),
        )
        await leader_election.wait_lose_leader()
        is_leader_after_not_renewed = leader_election.is_leader()
        cancel_group(tg)

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
        async with asyncio.TaskGroup() as tg:
            await start_task(tg, leader_election)
            await leader_election.wait_for_leader()
            mocker.patch.object(
                leader_election,
                "_try_acquire_or_renew",
                side_effect=Exception("Unexpected Exception"),
            )
            await leader_election.wait_lose_leader()

    # Act / Assert
    with pytest.raises(ExceptionGroup):
        await leader_election_unexpected_exception()


async def test_release_on_cancel(
    backend: SyncBackend, leader_election: LeaderElection, mocker: MockerFixture
) -> None:
    """Test leader election on worker that releases the lock on cancel."""
    # Arrange
    spy_release = mocker.spy(backend, "release")

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()
        cancel_group(tg)
    await leader_election.wait_lose_leader()

    # Assert
    spy_release.assert_called_once()


async def test_release_error_ignored(
    backend: SyncBackend,
    leader_election: LeaderElection,
    mocker: MockerFixture,
) -> None:
    """Test leader election on worker that ignores release error."""
    # Arrange
    mocker.patch.object(
        backend, "release", side_effect=Exception("Backend Unreachable")
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()
        cancel_group(tg)
    await leader_election.wait_lose_leader()


async def test_only_one_leader(leader_elections: list[LeaderElection]) -> None:
    """Test leader election on multiple workers ensuring only one leader is elected."""
    # Act
    leaders_before_start = [
        leader_election.is_leader() for leader_election in leader_elections
    ]
    async with asyncio.TaskGroup() as tg:
        for leader_election in leader_elections:
            await start_task(tg, leader_election)
        await wait_first_leader(leader_elections)
        leaders_after_start = [
            leader_election.is_leader() for leader_election in leader_elections
        ]
        cancel_group(tg)
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
    async with asyncio.TaskGroup() as workers_tg:
        async with asyncio.TaskGroup() as worker1_tg:
            await start_task(worker1_tg, leader_elections[WORKER_1])
            await leader_elections[WORKER_1].wait_for_leader()
            leaders_after_leader_election1_start = [
                leader_election.is_leader()
                for leader_election in leader_elections
            ]

            for leader_election in leader_elections:
                await start_task(workers_tg, leader_election)
            leaders_after_all_start = [
                leader_election.is_leader()
                for leader_election in leader_elections
            ]
            cancel_group(worker1_tg)

        await leader_elections[WORKER_1].wait_lose_leader()

        await wait_first_leader(leader_elections)
        leaders_after_leader_election1_down = [
            leader_election.is_leader() for leader_election in leader_elections
        ]
        cancel_group(workers_tg)

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
    backend: SyncBackend,
    caplog: pytest.LogCaptureFixture,
    mocker: MockerFixture,
) -> None:
    """Test leader election on worker with error cooldown."""
    # Arrange: build two leader elections with distinct error_interval values
    # so we can compare the log-throttling behaviour between them.
    caplog.set_level("ERROR")
    leader_election_high_cooldown = LeaderElection.from_config(
        LEADER_NAME,
        LeaderElectionConfig(
            worker="worker_high",
            lease_duration=0.02,
            renew_deadline=0.015,
            retry_interval=0.005,
            error_interval=1,
            backend_timeout=0.005,
        ),
        backend=backend,
    )
    leader_election_low_cooldown = LeaderElection.from_config(
        LEADER_NAME,
        LeaderElectionConfig(
            worker="worker_low",
            lease_duration=0.02,
            renew_deadline=0.015,
            retry_interval=0.005,
            error_interval=0.001,
            backend_timeout=0.005,
        ),
        backend=backend,
    )
    mocker.patch.object(
        backend, "acquire", side_effect=Exception("Backend Unreachable")
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election_high_cooldown)
        await sleep(0.01)
        cancel_group(tg)
    leader_election1_nb_errors = sum(
        1 for record in caplog.records if record.levelname == "ERROR"
    )
    caplog.clear()

    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election_low_cooldown)
        await sleep(0.01)
        cancel_group(tg)
    leader_election2_nb_errors = sum(
        1 for record in caplog.records if record.levelname == "ERROR"
    )

    # Assert
    assert leader_election1_nb_errors == 1
    assert leader_election2_nb_errors >= 1


# --- Guard ---


async def test_guard_raises_would_block_when_not_leader(
    leader_election: LeaderElection,
) -> None:
    """Test guard raises WouldBlock when the worker is not the leader."""
    # Arrange
    guard = leader_election.guard()

    # Act / Assert
    with pytest.raises(WouldBlock):
        async with guard:
            pass


async def test_guard_succeeds_when_leader(
    leader_election: LeaderElection,
) -> None:
    """Test guard succeeds when the worker is the leader."""
    # Arrange
    guard = leader_election.guard()
    entered = False

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()
        async with guard:
            entered = True
        cancel_group(tg)

    # Assert
    assert entered is True


async def test_guard_raises_would_block_after_losing_leadership(
    leader_election: LeaderElection,
) -> None:
    """Test guard raises WouldBlock after leadership is lost."""
    # Arrange
    guard = leader_election.guard()

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()
        cancel_group(tg)

    await leader_election.wait_lose_leader()

    # Assert
    with pytest.raises(WouldBlock):
        async with guard:
            pass


# --- reconfigure ---


async def test_leader_reconfigure_swaps_config(
    leader_election: LeaderElection,
) -> None:
    """Reconfigure publishes the new config."""
    new_config = leader_election.config.model_copy(
        update={"lease_duration": 0.04, "renew_deadline": 0.03},
    )

    await leader_election.reconfigure(new_config)

    assert leader_election.config == new_config


async def test_leader_reconfigure_same_config_is_noop(
    leader_election: LeaderElection,
) -> None:
    """Equal configs short-circuit."""
    same = leader_election.config.model_copy()

    await leader_election.reconfigure(same)

    assert leader_election.config == same


async def test_leader_reconfigure_rejects_worker_change(
    leader_election: LeaderElection,
) -> None:
    """Changing `worker` is not allowed: the lease is held under that token."""
    new_config = leader_election.config.model_copy(
        update={"worker": "other-worker"},
    )

    with pytest.raises(ValueError, match="cannot change worker"):
        await leader_election.reconfigure(new_config)


async def test_leader_reconfigure_takes_effect_on_next_iteration(
    leader_election: LeaderElection,
) -> None:
    """A swap during the renew loop is observed on the next read."""
    new_error_interval = 0.5
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()

        new_config = leader_election.config.model_copy(
            update={"error_interval": new_error_interval},
        )
        await leader_election.reconfigure(new_config)

        assert leader_election.config.error_interval == new_error_interval

        cancel_group(tg)
