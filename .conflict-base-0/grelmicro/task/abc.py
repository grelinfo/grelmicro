"""Grelmicro Task Synchronization Abstract Base Classes and Protocols."""

from typing import Protocol

from anyio import TASK_STATUS_IGNORED
from anyio.abc import TaskStatus
from typing_extensions import runtime_checkable


@runtime_checkable
class Task(Protocol):
    """Task Protocol.

    A task that runs in background in the async event loop.
    """

    @property
    def name(self) -> str:
        """Name to uniquely identify the task."""
        ...

    async def __call__(
        self,
        *,
        task_status: TaskStatus[None] = TASK_STATUS_IGNORED,
    ) -> None:
        """Run the task.

        This is the entry point of the task to be run in the async event loop.
        """
        ...
