"""Test Samples for the Task Component."""

from types import TracebackType
from typing import Self

from anyio import TASK_STATUS_IGNORED, Condition, Event, WouldBlock, sleep
from anyio.abc import TaskStatus
from typer import echo

from grelmicro.sync.abc import Synchronization
from grelmicro.task.abc import Task

condition = Condition()

# Shared state for e2e TaskLock tests
e2e_event_1: Event = Event()
e2e_event_2: Event = Event()
e2e_counter: dict[str, int] = {"worker_1": 0, "worker_2": 0}


def test1() -> None:
    """Test Function."""
    echo("test1")


def test2() -> None:
    """Test Function."""


def test3(test: str = "test") -> None:  # noqa: PT028
    """Test Function."""


async def notify() -> None:
    """Test Function that notifies the condition."""
    async with condition:
        condition.notify()


async def always_fail() -> None:
    """Test Function that always fails."""
    msg = "Test Error"
    raise ValueError(msg)


async def set_event_1() -> None:
    """Set e2e_event_1."""
    e2e_event_1.set()


async def set_event_2() -> None:
    """Set e2e_event_2."""
    e2e_event_2.set()


async def worker_1_hold() -> None:
    """Set e2e_event_1 then hold."""
    e2e_counter["worker_1"] += 1
    e2e_event_1.set()
    await sleep(10)


async def noop() -> None:
    """Do nothing."""


class SimpleClass:
    """Test Class."""

    def method(self) -> None:
        """Test Method."""

    @staticmethod
    def static_method() -> None:
        """Test Static Method."""


class EventTask(Task):
    """Test Scheduled Task with Event."""

    def __init__(self, *, event: Event | None = None) -> None:
        """Initialize the event task."""
        self._event = event or Event()

    @property
    def name(self) -> str:
        """Return the task name."""
        return "event_task"

    async def __call__(
        self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED
    ) -> None:
        """Run the task that sets the event."""
        task_status.started()
        self._event.set()


class WouldBlockLock(Synchronization):
    """Lock that always raises WouldBlock."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""
        msg = "Already locked"
        raise WouldBlock(msg)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit the synchronization primitive."""


class BadLock(Synchronization):
    """Bad Lock."""

    async def __aenter__(self) -> Self:
        """Enter the synchronization primitive."""
        msg = "Bad Lock"
        raise ValueError(msg)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit the synchronization primitive."""
