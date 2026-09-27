"""Test Tasks."""

import asyncio
from asyncio import Event

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, LeaderElection
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
)
from grelmicro.errors import OutOfContextError
from grelmicro.task import LeaderNotRegisteredError, TaskRouter, Tasks
from grelmicro.task.errors import (
    TaskStartOperationError,
)
from tests.task.samples import EventTask

pytestmark = [pytest.mark.timeout(10)]

# Module-level interval task functions (the interval decorator rejects
# nested functions). `_drain_events` records whether the running
# iteration was interrupted by a cancellation.
_drain_events: list[str] = []


async def _drain_work() -> None:
    """Append a marker, yielding once to model in-flight work."""
    import asyncio  # noqa: PLC0415

    try:
        _drain_events.append("run")
        await asyncio.sleep(0)
    except asyncio.CancelledError:  # pragma: no cover - drain should avoid this
        _drain_events.append("cancelled")
        raise


async def _noop_work() -> None:
    """Do nothing; used to exercise the interruptible interval sleep."""


def test_tasks_init() -> None:
    """Test Tasks Initialization."""
    # Act
    task = EventTask()
    app = Tasks()
    app_with_tasks = Tasks(tasks=[task])
    # Assert
    assert app.tasks == []
    assert app_with_tasks.tasks == [task]


async def test_tasks_context() -> None:
    """Test Tasks Context."""
    # Arrange
    event = Event()
    task = EventTask(event=event)
    app = Tasks(tasks=[task])

    # Act
    event_before = event.is_set()
    async with app:
        event_in_context = event.is_set()

    # Assert
    assert event_before is False
    assert event_in_context is True


@pytest.mark.parametrize("auto_start", [True, False])
async def test_tasks_auto_start_disabled(*, auto_start: bool) -> None:
    """Test Tasks Auto Start Disabled."""
    # Arrange
    event = Event()
    task = EventTask(event=event)
    app = Tasks(auto_start=auto_start, tasks=[task])

    # Act
    event_before = event.is_set()
    async with app:
        event_in_context = event.is_set()

    # Assert
    assert event_before is False
    assert event_in_context is auto_start


async def test_tasks_already_started_error() -> None:
    """Starting twice reports the start mistake, not an add mistake."""
    # Arrange
    app = Tasks()

    # Act / Assert
    async with app:
        with pytest.raises(TaskStartOperationError, match="twice"):
            await app.start()


async def test_tasks_start_surfaces_early_task_failure() -> None:
    """A task that raises before setting ``ready`` surfaces the error from start()."""
    import asyncio  # noqa: PLC0415

    class FailingTask:
        @property
        def name(self) -> str:
            return "boom"

        async def __call__(
            self,
            *,
            ready: asyncio.Future[None] | None = None,
            stop: asyncio.Event | None = None,
        ) -> None:
            del ready, stop
            msg = "early failure"
            raise RuntimeError(msg)

    app = Tasks(auto_start=False, tasks=[FailingTask()])

    with pytest.raises(BaseExceptionGroup) as exc_info:
        async with app:
            await app.start()

    assert any(
        isinstance(e, RuntimeError) and str(e) == "early failure"
        for e in exc_info.value.exceptions
    )


async def test_tasks_start_surfaces_early_clean_exit() -> None:
    """A task that returns without setting ``ready`` raises instead of deadlocking."""
    import asyncio  # noqa: PLC0415

    class SilentTask:
        @property
        def name(self) -> str:
            return "silent"

        async def __call__(
            self,
            *,
            ready: asyncio.Future[None] | None = None,
            stop: asyncio.Event | None = None,
        ) -> None:
            del ready, stop

    app = Tasks(auto_start=False, tasks=[SilentTask()])

    with pytest.raises(BaseExceptionGroup) as exc_info:
        async with app:
            await app.start()

    assert any(
        isinstance(e, RuntimeError) and "before signaling readiness" in str(e)
        for e in exc_info.value.exceptions
    )


async def test_tasks_cancels_running_tasks_on_exit() -> None:
    """Long-running tasks are cancelled on context exit."""
    import asyncio  # noqa: PLC0415

    cancelled = asyncio.Event()

    class LongRunningTask:
        """A task that ignores the stop signal, so it must be force-cancelled."""

        @property
        def name(self) -> str:
            return "long"

        async def __call__(
            self,
            *,
            ready: asyncio.Future[None] | None = None,
            stop: asyncio.Event | None = None,
        ) -> None:
            del stop
            if ready is not None:
                ready.set_result(None)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    # The task never observes the stop signal, so it is force-cancelled
    # once the short drain window elapses.
    async with Tasks(tasks=[LongRunningTask()], shutdown_timeout=0.05):
        pass

    assert cancelled.is_set()


async def test_tasks_out_of_context_errors() -> None:
    """Test Tasks Out of Context Errors."""
    # Arrange
    app = Tasks()

    # Act / Assert
    with pytest.raises(OutOfContextError):
        await app.start()

    with pytest.raises(OutOfContextError):
        await app.__aexit__(None, None, None)


async def test_tasks_shutdown_timeout_negative_rejected() -> None:
    """A negative shutdown_timeout is rejected at construction."""
    with pytest.raises(ValueError, match="shutdown_timeout"):
        Tasks(shutdown_timeout=-1)


async def test_tasks_drains_cooperative_task_without_cancel() -> None:
    """An interval task finishes its iteration on stop and is not cancelled."""
    import asyncio  # noqa: PLC0415

    _drain_events.clear()
    tasks = Tasks(auto_start=False, shutdown_timeout=5)
    tasks.every(seconds=0.01)(_drain_work)

    async with tasks:
        await tasks.start()
        await asyncio.sleep(0.05)  # let it run a few iterations

    # Exited well under shutdown_timeout, and the running iteration was
    # never interrupted by a cancellation.
    assert "run" in _drain_events
    assert "cancelled" not in _drain_events


async def test_tasks_interval_wakes_promptly_on_stop() -> None:
    """A task sleeping on a long interval breaks immediately on shutdown."""
    import asyncio  # noqa: PLC0415

    tasks = Tasks(auto_start=False, shutdown_timeout=5)
    tasks.every(seconds=3600)(_noop_work)  # would otherwise sleep an hour

    async with asyncio.timeout(
        2
    ):  # fails loudly if the sleep is not interruptible
        async with tasks:
            await tasks.start()
            await asyncio.sleep(0.02)


async def _gated_work() -> None:
    """Do nothing, gated on a leader election."""


def _gated_app(*uses: Tasks) -> Grelmicro:
    """Build an app with memory coordination and the given task managers."""
    return Grelmicro(
        uses=[
            Coordination(
                lock=MemoryLockAdapter(),
                election=MemoryLeaderElectionAdapter(),
            ),
            *uses,
        ]
    )


def _gate(tasks: TaskRouter, election: LeaderElection, kind: str) -> None:
    """Register `_gated_work` on `tasks`, gated on `election`."""
    if kind == "every":
        tasks.every(seconds=60, gate=election)(_gated_work)
    else:
        tasks.cron("0 0 1 1 *", gate=election)(_gated_work)


@pytest.mark.parametrize("kind", ["every", "cron"])
async def test_tasks_refuse_a_leader_gate_no_tasks_runs(kind: str) -> None:
    """A task gated on an election that no Tasks runs is refused at start.

    The election renews its lease only while it runs as a task. Run
    nowhere, it never acquires, so every fire would be skipped silently.
    """
    election = LeaderElection("svc")
    tasks = Tasks()
    _gate(tasks, election, kind)

    with pytest.raises(LeaderNotRegisteredError) as info:
        async with _gated_app(tasks):
            pass  # pragma: no cover

    message = str(info.value)
    assert "'tests.task.test_tasks:_gated_work'" in message
    assert "'svc'" in message
    assert "tasks.add_task(election)" in message
    assert not tasks.started()


async def test_tasks_start_a_standalone_leader_gate_before_its_election() -> (
    None
):
    """Outside an app, a gated Tasks opened before the one running the election starts."""
    election = LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
    gated = Tasks()
    _gate(gated, election, "cron")

    async with gated, Tasks(tasks=[election]):
        assert election.is_running()


async def test_tasks_start_a_standalone_leader_gate_run_by_hand_after() -> None:
    """Outside an app, an election driven by hand after the Tasks opens starts."""
    election = LeaderElection("svc", backend=MemoryLeaderElectionAdapter())
    tasks = Tasks()
    _gate(tasks, election, "cron")

    async with tasks, asyncio.TaskGroup() as tg:
        stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tg.create_task(election(ready=ready, stop=stop))
        await ready
        assert election.is_running()
        stop.set()


async def test_tasks_skip_the_leader_check_for_tasks_the_app_does_not_hold() -> (
    None
):
    """A Tasks opened inside an app block, but not held by it, is not checked."""
    election = LeaderElection("svc")
    tasks = Tasks()
    _gate(tasks, election, "cron")

    async with _gated_app(), tasks:
        assert tasks.started()


async def test_tasks_start_a_leader_gate_registered_after_the_task() -> None:
    """An election added to the same Tasks after the task it gates runs."""
    election = LeaderElection("svc")
    tasks = Tasks()
    _gate(tasks, election, "every")
    tasks.add_task(election)

    async with _gated_app(tasks):
        assert election.is_running()


async def test_tasks_start_a_leader_gate_from_an_included_router() -> None:
    """A router's gated task passes when the Tasks holding it runs the election."""
    election = LeaderElection("svc")
    router = TaskRouter()
    _gate(router, election, "cron")
    tasks = Tasks(tasks=[election])
    tasks.include_router(router)

    async with _gated_app(tasks):
        assert election.is_running()


async def test_tasks_start_a_leader_gate_run_by_another_tasks() -> None:
    """An election another Tasks of the app runs passes, whichever opens first."""
    election = LeaderElection("svc")
    gated = Tasks()
    _gate(gated, election, "every")

    async with _gated_app(gated, Tasks(tasks=[election])):
        assert election.is_running()


async def test_tasks_start_a_leader_gate_already_running() -> None:
    """An election driven by hand before the Tasks opens passes."""
    election = LeaderElection("svc")
    tasks = Tasks()
    _gate(tasks, election, "every")

    async with _gated_app(), asyncio.TaskGroup() as tg:
        stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tg.create_task(election(ready=ready, stop=stop))
        await ready
        async with tasks:
            assert tasks.started()
        stop.set()
