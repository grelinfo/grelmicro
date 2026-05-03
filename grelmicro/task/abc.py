"""Task Abstract Base Classes and Protocols."""

import asyncio
from typing import Protocol, runtime_checkable


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
        ready: asyncio.Future[None] | None = None,
    ) -> None:
        """Run the task.

        ``ready`` is a Future the task should resolve once it has reached
        a steady state. The parent uses it to know when start-up has
        finished. ``None`` means the parent does not wait.

        This is the entry point of the task to be run in the async event loop.
        """
        ...
