"""Test Tasks."""

import warnings
from asyncio import Event

import pytest

from grelmicro.errors import OutOfContextError
from grelmicro.task import Tasks
from grelmicro.task.errors import TaskAddOperationError
from tests.task.samples import EventTask

pytestmark = [pytest.mark.timeout(10)]


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
    """Test Tasks Already Started Warning."""
    # Arrange
    app = Tasks()

    # Act / Assert
    async with app:
        with pytest.raises(TaskAddOperationError):
            await app.start()


async def test_tasks_start_surfaces_early_task_failure() -> None:
    """A task that raises before setting ``ready`` surfaces the error from start()."""
    import asyncio  # noqa: PLC0415

    class FailingTask:
        @property
        def name(self) -> str:
            return "boom"

        async def __call__(
            self, *, ready: asyncio.Future[None] | None = None
        ) -> None:
            del ready
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
            self, *, ready: asyncio.Future[None] | None = None
        ) -> None:
            del ready

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
        @property
        def name(self) -> str:
            return "long"

        async def __call__(
            self, *, ready: asyncio.Future[None] | None = None
        ) -> None:
            if ready is not None:
                ready.set_result(None)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async with Tasks(tasks=[LongRunningTask()]):
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


def test_task_manager_alias_emits_deprecation_warning() -> None:
    """Accessing TaskManager from grelmicro.task emits DeprecationWarning once."""
    import grelmicro.task as task_module  # noqa: PLC0415

    task_module.__dict__.pop("TaskManager", None)

    with pytest.warns(DeprecationWarning, match="TaskManager is deprecated"):
        alias = task_module.TaskManager

    assert alias is Tasks
    # Cached after first access: no further warnings.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert task_module.TaskManager is Tasks


def test_unknown_attribute_raises_attribute_error() -> None:
    """Unknown attributes on grelmicro.task raise AttributeError."""
    import grelmicro.task as task_module  # noqa: PLC0415

    with pytest.raises(AttributeError, match="has no attribute 'DoesNotExist'"):
        task_module.DoesNotExist  # noqa: B018
