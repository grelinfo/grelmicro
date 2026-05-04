"""Test utilities for Lock."""

import asyncio
from asyncio import Event

from grelmicro.sync._base import BaseLock
from tests.task._helpers import cancel_group


async def wait_first_acquired(locks: list[BaseLock]) -> None:
    """Wait for the first lock to be acquired."""

    async def wrapper(lock: BaseLock, event: Event) -> None:
        """Send event when lock is acquired."""
        await asyncio.wait_for(lock.acquire(), 1)
        event.set()

    async def _run() -> None:
        async with asyncio.TaskGroup() as task_group:
            event = Event()
            for lock in locks:
                task_group.create_task(wrapper(lock, event))
            await event.wait()
            cancel_group(task_group)

    await asyncio.wait_for(_run(), 1)
