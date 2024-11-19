"""Test utilities for Lock."""

from anyio import Event, create_task_group, fail_after

from grelmicro.abc.lock import BaseLock


async def wait_first_acquired(locks: list[BaseLock]) -> None:
    """Wait for the first lock to be acquired."""

    async def wrapper(lock: BaseLock, event: Event) -> None:
        """Send event when lock is acquired."""
        with fail_after(1):
            await lock.acquire()
            event.set()

    with fail_after(1):
        async with create_task_group() as task_group:
            event = Event()
            for lock in locks:
                task_group.start_soon(wrapper, lock, event)
            await event.wait()
            task_group.cancel_scope.cancel()
