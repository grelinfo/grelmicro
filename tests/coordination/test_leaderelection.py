"""Test leader election."""

import asyncio
import math
from asyncio import Event, sleep
from contextlib import suppress

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

import grelmicro.coordination._base as base_module
import grelmicro.coordination.leaderelection as le_module
from grelmicro.coordination._protocol import LeaderElectionBackend
from grelmicro.coordination.errors import CoordinationSettingsValidationError
from grelmicro.coordination.leaderelection import (
    LeaderElection,
    LeaderElectionConfig,
)
from grelmicro.coordination.memory import MemoryLeaderElectionAdapter
from grelmicro.errors import OutOfContextError
from grelmicro.errors import WouldBlockError as WouldBlock
from tests.task._helpers import cancel_group, start_task

LEADER_NAME = "test_leader_election"
BACKEND_LOCK_NAME = f"leader:{LEADER_NAME}"
WORKERS = 4
WORKER_1 = 0
WORKER_2 = 1
TEST_TIMEOUT = 1
LEASE_DURATION = 0.02
RENEW_DEADLINE = 0.015

pytestmark = [pytest.mark.timeout(TEST_TIMEOUT)]


@pytest.fixture
def backend() -> LeaderElectionBackend:
    """Return Memory Synchronization Backend."""
    return MemoryLeaderElectionAdapter()


@pytest.fixture
def configs() -> list[LeaderElectionConfig]:
    """Leader election Config."""
    return [
        LeaderElectionConfig(
            worker=f"worker_{i}",
            lease_duration=LEASE_DURATION,
            renew_deadline=RENEW_DEADLINE,
            retry_interval=0.005,
            error_interval=0.01,
            backend_timeout=0.005,
        )
        for i in range(WORKERS)
    ]


@pytest.fixture
def leader_elections(
    backend: LeaderElectionBackend, configs: list[LeaderElectionConfig]
) -> list[LeaderElection]:
    """Leader elections."""
    return [
        LeaderElection.from_config(LEADER_NAME, configs[i], backend=backend)
        for i in range(WORKERS)
    ]


@pytest.fixture
def leader_election(
    backend: LeaderElectionBackend, configs: list[LeaderElectionConfig]
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
        "retry_jitter": 0.1,
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
        "retry_jitter": 0.1,
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
    backend: LeaderElectionBackend, leader_election: LeaderElection
) -> None:
    """Test LeaderElection uses prefixed key on the backend."""
    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, leader_election)
        await leader_election.wait_for_leader()

        # Assert - backend record should be stored under the prefixed key
        assert await backend.get(name=BACKEND_LOCK_NAME) is not None
        # Raw name should NOT hold a record
        assert await backend.get(name=LEADER_NAME) is None
        # The election exposes the live record of the current leader.
        record = leader_election.record
        assert record is not None
        assert record.holder == str(leader_election.config.worker)
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
            "acquire_or_renew",
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
    backend: LeaderElectionBackend,
    leader_election: LeaderElection,
    mocker: MockerFixture,
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
    backend: LeaderElectionBackend,
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
        # Count confirmed leaders, not the advisory `is_leader()`. During a
        # lease handoff a just-demoted worker still reports `is_leader()` True
        # until its renew deadline elapses, so two could appear at once. A
        # worker that lost the lease has a confirmation at least one
        # `lease_duration` old, which `is_leader_confirmed_within` excludes.
        leaders_after_start = [
            leader_election.is_leader_confirmed_within(RENEW_DEADLINE)
            for leader_election in leader_elections
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
    backend: LeaderElectionBackend,
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
        backend,
        "acquire_or_renew",
        side_effect=Exception("Backend Unreachable"),
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

    with pytest.raises(
        CoordinationSettingsValidationError, match="worker is immutable"
    ):
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


async def test_leader_election_stops_gracefully_on_stop_event(
    leader_election: LeaderElection,
    backend: LeaderElectionBackend,
) -> None:
    """Setting the stop event breaks the loop and releases leadership."""
    stop = Event()
    async with asyncio.TaskGroup() as tg:
        handle = await start_task(tg, leader_election, stop=stop)
        await leader_election.wait_for_leader()
        assert leader_election.is_leader() is True
        stop.set()  # request graceful shutdown
    # The loop broke on its own, without a cancellation.
    assert handle.done()
    assert not handle.cancelled()
    # Leadership was released on the backend, so another worker can take it.
    record = await backend.acquire_or_renew(
        name=leader_election._lock_name,
        token="other",
        duration=1,
    )
    assert record.holder == "other"


async def test_graceful_stop_releases_app_resolved_backend() -> None:
    """Graceful stop releases the backend resolved from a Coordination app.

    With no `backend=`, the election resolves through the registered
    `Coordination` component. On stop the lease must be vacated, so another
    token can acquire immediately instead of waiting the lease duration.
    """
    from grelmicro import Grelmicro  # noqa: PLC0415
    from grelmicro.coordination import Coordination  # noqa: PLC0415

    # Arrange: an app-resolved election backend, no explicit backend= on the
    # LeaderElection.
    backend = MemoryLeaderElectionAdapter()
    micro = Grelmicro(uses=[Coordination(election=backend)])
    leader_election = LeaderElection(
        LEADER_NAME,
        worker="worker_app",
        lease_duration=LEASE_DURATION,
        renew_deadline=RENEW_DEADLINE,
        retry_interval=0.005,
        error_interval=0.01,
        backend_timeout=0.005,
    )

    stop = Event()
    async with micro:
        async with asyncio.TaskGroup() as tg:
            handle = await start_task(tg, leader_election, stop=stop)
            await leader_election.wait_for_leader()
            assert leader_election.is_leader() is True
            stop.set()  # request graceful shutdown
        # The loop broke on its own, without a cancellation.
        assert handle.done()
        assert not handle.cancelled()
        # Leadership was released, so another worker can take it immediately.
        record = await backend.acquire_or_renew(
            name=leader_election._lock_name,
            token="other",
            duration=1,
        )
        assert record.holder == "other"


async def test_release_returns_quietly_when_app_context_gone() -> None:
    """`_release` is a no-op when no backend resolves out of context.

    With no `backend=` and no active app, resolving the backend raises
    `OutOfContextError`. There is nothing to release, so `_release` returns
    quietly instead of propagating.
    """
    # Arrange: no explicit backend, no active app context.
    leader_election = LeaderElection(LEADER_NAME, worker="worker_gone")

    # Act / Assert: must not raise.
    await leader_election._release()


async def test_graceful_stop_awaits_release_before_returning(
    backend: LeaderElectionBackend,
    mocker: MockerFixture,
) -> None:
    """Graceful stop blocks on `_release()` until the backend release returns."""
    # Arrange: a generous backend_timeout so the gated release, not the
    # timeout, decides when `_release` completes.
    leader_election = LeaderElection.from_config(
        LEADER_NAME,
        LeaderElectionConfig(
            worker="worker_graceful",
            lease_duration=0.7,
            renew_deadline=0.6,
            retry_interval=0.01,
            error_interval=0.01,
            backend_timeout=0.5,
        ),
        backend=backend,
    )

    # Gate the backend release so it cannot complete until allowed.
    release_called = Event()
    allow_release = Event()
    real_release = backend.release

    async def gated_release(*, name: str, token: str) -> bool:
        release_called.set()
        await allow_release.wait()
        return await real_release(name=name, token=token)

    mocker.patch.object(backend, "release", side_effect=gated_release)

    stop = Event()
    async with asyncio.TaskGroup() as tg:
        handle = await start_task(tg, leader_election, stop=stop)
        await leader_election.wait_for_leader()
        stop.set()  # request graceful shutdown

        # The loop must reach `_release` and wait for it: the call started
        # but the task has not returned because release is still gated.
        await release_called.wait()
        assert handle.done() is False

        # Releasing the gate lets `_release` finish and the loop return.
        allow_release.set()
        await handle

    assert handle.done() is True
    assert handle.cancelled() is False


async def test_leader_election_without_jitter(
    backend: LeaderElectionBackend,
) -> None:
    """A zero `retry_jitter` renews on the fixed interval."""
    # Arrange
    election = LeaderElection(
        "test-no-jitter",
        backend=backend,
        worker="worker_nj",
        lease_duration=LEASE_DURATION,
        renew_deadline=LEASE_DURATION * 0.66,
        retry_interval=LEASE_DURATION * 0.33,
        retry_jitter=0,
        backend_timeout=LEASE_DURATION * 0.5,
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        await start_task(tg, election)
        is_leader_inside = election.is_leader()
        cancel_group(tg)
    await election.wait_lose_leader()

    # Assert
    assert is_leader_inside is True


async def test_renew_loop_jitter_formula_exact_sleep(
    leader_election: LeaderElection,
    mocker: MockerFixture,
) -> None:
    """The renew sleep equals retry_interval * (1 + jitter * (2*_random - 1))."""
    # Arrange: pin randomness and capture the interval, then stop after one
    # iteration so the loop unwinds.
    random_value = 0.75
    mocker.patch.object(base_module, "_random", return_value=random_value)
    recorded: list[float] = []

    async def fake_sleep_or_stop(seconds: float, _stop: object) -> bool:
        recorded.append(seconds)
        return True

    mocker.patch.object(
        le_module, "sleep_or_stop", side_effect=fake_sleep_or_stop
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        handle = await start_task(tg, leader_election, stop=Event())
        await handle

    # Assert
    config = leader_election.config
    expected = config.retry_interval * (
        1.0 + config.retry_jitter * (2.0 * random_value - 1.0)
    )
    assert recorded == [pytest.approx(expected)]


async def test_renew_loop_no_jitter_exact_sleep(
    backend: LeaderElectionBackend,
    mocker: MockerFixture,
) -> None:
    """With retry_jitter=0 the renew sleep equals retry_interval exactly."""
    # Arrange
    retry_interval = LEASE_DURATION * 0.33
    election = LeaderElection(
        "test-no-jitter-exact",
        backend=backend,
        worker="worker_nj",
        lease_duration=LEASE_DURATION,
        renew_deadline=LEASE_DURATION * 0.66,
        retry_interval=retry_interval,
        retry_jitter=0,
        backend_timeout=LEASE_DURATION * 0.5,
    )
    recorded: list[float] = []

    async def fake_sleep_or_stop(seconds: float, _stop: object) -> bool:
        recorded.append(seconds)
        return True

    mocker.patch.object(
        le_module, "sleep_or_stop", side_effect=fake_sleep_or_stop
    )

    # Act
    async with asyncio.TaskGroup() as tg:
        handle = await start_task(tg, election, stop=Event())
        await handle

    # Assert
    assert recorded == [pytest.approx(retry_interval)]


async def test_guard_error_carries_name(
    leader_election: LeaderElection,
) -> None:
    """The guard `WouldBlockError` names the election in its message."""
    guard = leader_election.guard()
    with pytest.raises(WouldBlock) as exc:
        async with guard:
            pass
    assert LEADER_NAME in str(exc.value)


async def test_leaderelection_backend_out_of_context() -> None:
    """A `LeaderElection` with no backend and no active app raises `OutOfContextError`."""
    election = LeaderElection("out-of-context", worker="worker")
    with pytest.raises(
        OutOfContextError, match="LeaderElection\\('out-of-context'\\)"
    ):
        _ = election.backend


# --- lead() ------------------------------------------------------------------


@pytest.fixture
def stable_leader(backend: LeaderElectionBackend) -> LeaderElection:
    """Return a leader whose lease never lapses, so loss is driven manually.

    Lease and renew deadline are large enough that `is_leader()` stays
    `True` after a manual `_update_state(is_leader=True)`, letting each
    `lead()` test control loss explicitly with no renew-loop racing.
    """
    config = LeaderElectionConfig(
        worker="worker_lead",
        lease_duration=100,
        renew_deadline=90,
        retry_interval=1,
        error_interval=30,
        backend_timeout=5,
    )
    return LeaderElection.from_config(LEADER_NAME, config, backend=backend)


async def test_lead_runs_while_leader_and_returns_result(
    stable_leader: LeaderElection,
) -> None:
    """`lead` runs the body while leader and returns its result."""
    await stable_leader._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )

    async def work() -> str:
        return "done"

    assert await stable_leader.lead(work) == "done"


async def test_lead_passes_positional_args(
    stable_leader: LeaderElection,
) -> None:
    """`lead` forwards positional arguments to the body."""
    await stable_leader._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )

    async def add(left: int, right: int) -> int:
        return left + right

    left, right = 2, 3
    assert await stable_leader.lead(add, left, right) == left + right


async def test_lead_propagates_body_exception(
    stable_leader: LeaderElection,
) -> None:
    """An exception raised by the body propagates out of `lead`."""
    await stable_leader._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )

    async def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await stable_leader.lead(boom)


async def test_lead_waits_for_leadership_before_running(
    stable_leader: LeaderElection,
) -> None:
    """`lead` blocks until leadership is acquired, then runs the body."""
    started = Event()

    async def work() -> str:
        started.set()
        return "ran"

    async with asyncio.TaskGroup() as tg:
        task = tg.create_task(stable_leader.lead(work))
        await sleep(0)
        assert not started.is_set()  # not leader yet

        await stable_leader._update_state(
            is_leader=True, reason_if_no_more_leader=""
        )
        assert await task == "ran"

    assert started.is_set()


async def test_lead_cancels_body_on_leadership_loss(
    stable_leader: LeaderElection,
) -> None:
    """Losing leadership cancels the in-flight body and returns `None`."""
    await stable_leader._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )
    started = Event()
    cancelled = Event()

    async def work() -> None:
        started.set()
        try:
            await sleep(100)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with asyncio.TaskGroup() as tg:
        task = tg.create_task(stable_leader.lead(work))
        await started.wait()

        await stable_leader._update_state(
            is_leader=False, reason_if_no_more_leader="lost"
        )
        assert await task is None

    assert cancelled.is_set()


async def test_lead_repeat_reruns_after_reacquire(
    stable_leader: LeaderElection,
) -> None:
    """With `repeat=True` the body re-runs after leadership is re-acquired."""
    await stable_leader._update_state(
        is_leader=True, reason_if_no_more_leader=""
    )
    runs = 0
    running = Event()

    async def work() -> None:
        nonlocal runs
        runs += 1
        running.set()
        await sleep(100)

    first_run, second_run = 1, 2
    task = asyncio.ensure_future(stable_leader.lead(work, repeat=True))
    try:
        await running.wait()
        assert runs == first_run

        # Lose leadership: the body is cancelled and lead loops back.
        await stable_leader._update_state(
            is_leader=False, reason_if_no_more_leader="lost"
        )
        running.clear()
        await sleep(0)

        # Re-acquire: the body runs a second time.
        await stable_leader._update_state(
            is_leader=True, reason_if_no_more_leader=""
        )
        await running.wait()
        assert runs == second_run
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
