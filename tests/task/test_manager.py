"""Test Task Manager."""

import pytest
from anyio import Event

from grelmicro.errors import OutOfContextError
from grelmicro.task import TaskManager
from grelmicro.task.errors import TaskAlreadyStartedError
from tests.task.samples import EventTask

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


def test_task_manager_init() -> None:
    """Test Task Manager Initialization."""
    # Act
    task = EventTask()
    app = TaskManager()
    app_with_tasks = TaskManager(tasks=[task])
    # Assert
    assert app.tasks == []
    assert app_with_tasks.tasks == [task]


async def test_task_manager_context() -> None:
    """Test Task Manager Context."""
    # Arrange
    event = Event()
    task = EventTask(event=event)
    app = TaskManager(tasks=[task])

    # Act
    event_before = event.is_set()
    async with app:
        event_in_context = event.is_set()

    # Assert
    assert event_before is False
    assert event_in_context is True


@pytest.mark.parametrize("auto_start", [True, False])
async def test_task_manager_auto_start_disabled(*, auto_start: bool) -> None:
    """Test Task Manager Auto Start Disabled."""
    # Arrange
    event = Event()
    task = EventTask(event=event)
    app = TaskManager(auto_start=auto_start, tasks=[task])

    # Act
    event_before = event.is_set()
    async with app:
        event_in_context = event.is_set()

    # Assert
    assert event_before is False
    assert event_in_context is auto_start


async def test_task_manager_already_started_error() -> None:
    """Test Task Manager Already Started Warning."""
    # Arrange
    app = TaskManager()

    # Act / Assert
    async with app:
        with pytest.raises(TaskAlreadyStartedError):
            await app.start()


async def test_task_manager_out_of_context_errors() -> None:
    """Test Task Manager Out of Context Errors."""
    # Arrange
    app = TaskManager()

    # Act / Assert
    with pytest.raises(OutOfContextError):
        await app.start()

    with pytest.raises(OutOfContextError):
        await app.__aexit__(None, None, None)
