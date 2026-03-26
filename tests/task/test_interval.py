"""Test Interval Task."""

import warnings

import pytest
from anyio import create_task_group, sleep, sleep_forever
from pytest_mock import MockFixture

from grelmicro.task._interval import IntervalTask
from tests.task.samples import (
    BadLock,
    WouldBlockLock,
    always_fail,
    condition,
    notify,
    test1,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.timeout(10),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

SLEEP = 0.01


def test_interval_task_init() -> None:
    """Test Interval Task Initialization."""
    # Act
    task = IntervalTask(seconds=1, function=test1)
    # Assert
    assert task.name == "tests.task.samples:test1"


def test_interval_task_init_with_name() -> None:
    """Test Interval Task Initialization with Name."""
    # Act
    task = IntervalTask(seconds=1, function=test1, name="test1")
    # Assert
    assert task.name == "test1"


def test_interval_task_init_with_invalid_interval() -> None:
    """Test Interval Task Initialization with Invalid Interval."""
    # Act / Assert
    with pytest.raises(ValueError, match="seconds must be greater than 0"):
        IntervalTask(seconds=0, function=test1)


def test_interval_task_sync_deprecation_warning() -> None:
    """Test that sync= emits DeprecationWarning."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        IntervalTask(seconds=1, function=test1, sync=WouldBlockLock())

    assert len(w) == 1
    assert issubclass(w[0].category, DeprecationWarning)
    assert "sync" in str(w[0].message)
    assert "max_lock_seconds" in str(w[0].message)


async def test_interval_task_start() -> None:
    """Test Interval Task Start."""
    # Arrange
    task = IntervalTask(seconds=1, function=notify)
    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        async with condition:
            await condition.wait()
        tg.cancel_scope.cancel()


async def test_interval_task_execution_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Execution Error."""
    # Arrange
    task = IntervalTask(seconds=1, function=always_fail)
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


async def test_interval_task_would_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task WouldBlock logs at DEBUG, not ERROR."""
    # Arrange
    caplog.set_level("DEBUG")
    task = IntervalTask(seconds=1, function=notify, sync=WouldBlockLock())

    # Act
    async with create_task_group() as tg:
        await tg.start(task)
        await sleep(SLEEP)
        tg.cancel_scope.cancel()

    # Assert
    assert any(
        "Task skipped:" in record.message
        for record in caplog.records
        if record.levelname == "DEBUG"
    )
    assert not any(
        "Task synchronization error:" in record.message
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_interval_task_synchronization_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test Interval Task Synchronization Error."""
    # Arrange
    task = IntervalTask(seconds=1, function=notify, sync=BadLock())

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

    mocker.patch(
        "grelmicro.task._interval.sleep", side_effect=CustomBaseException
    )
    task = IntervalTask(seconds=1, function=test1)

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
