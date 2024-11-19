"""Test Interval Task."""

import pytest
from anyio import create_task_group, sleep, sleep_forever
from pytest_mock import MockFixture

from grelmicro.task._interval import IntervalTask
from tests.task.samples import (
    BadLock,
    always_fail,
    condition,
    notify,
    test1,
)

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]

INTERVAL = 0.1
SLEEP = 0.01


def test_interval_task_init() -> None:
    """Test Interval Task Initialization."""
    # Act
    task = IntervalTask(interval=1, function=test1)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_init_with_name() -> None:
    """Test Interval Task Initialization with Name."""
    # Act
    task = IntervalTask(interval=1, function=test1, name="test1")
    # Assert
    assert task.name == "test1"


def test_interval_task_init_with_invalid_interval() -> None:
    """Test Interval Task Initialization with Invalid Interval."""
    # Act / Assert
    with pytest.raises(ValueError, match="Interval must be greater than 0."):
        IntervalTask(interval=0, function=test1)


async def test_interval_task_start() -> None:
    """Test Interval Task Start."""
    # Arrange
    task = IntervalTask(interval=1, function=notify)
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        async with condition:
            await condition.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_execution_error(caplog: pytest.LogCaptureFixture) -> None:
    """Test Interval Task Execution Error."""
    # Arrange
    task = IntervalTask(interval=1, function=always_fail)
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(SLEEP)
        tg.cancel_scope.cancel()

    # Assert
    assert any(
        "Task execution error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_synchronization_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Synchronization Error."""
    # Arrange
    task = IntervalTask(interval=1, function=notify, sync=BadLock())

    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(SLEEP)
        tg.cancel_scope.cancel()

    # Assert
    assert any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_stop(
    caplog: pytest.LogCaptureFixture, mocker: MockFixture
) -> None:
    """Test Interval Task stop."""
    # Arrange
    caplog.set_level("INFO")

    class CustomBaseException(BaseException):
        pass

    mocker.patch("grelmicro.task._interval.sleep", side_effect=CustomBaseException)
    task = IntervalTask(interval=1, function=test1)

    async def leader_election_during_runtime_error() -> None:
        async with create_task_group() as tg:
            await tg.start(task)
            await sleep_forever()

    # Act
    with pytest.raises(BaseExceptionGroup):
        await leader_election_during_runtime_error()

    # Assert
    assert any(
        "Task stopped:" in record.message
        for record in caplog.records
        if record.levelname == "INFO"
    )
