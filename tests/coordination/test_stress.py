"""Stress tests for the in-memory synchronization primitives.

Opt-in only. Every test carries both ``stress`` and ``slow`` so the
default unit/coverage job (``-m "not integration and not slow and not
stress"``) skips them. Run them with ``-m stress``.

Op-counts are bounded so each test settles in well under a second.
These are fast in-memory churn tests, not real-time endurance runs.
"""

import asyncio

import pytest

from grelmicro.coordination.lock import Lock
from grelmicro.coordination.memory import MemoryLockAdapter

pytestmark = [pytest.mark.stress, pytest.mark.slow]

CONTENDERS = 50
STARTUP_CYCLES = 2_000


async def test_lock_contention_many_acquirers() -> None:
    """Many concurrent acquirers serialize on one in-memory lock."""
    held = 0
    max_held = 0

    async with MemoryLockAdapter() as backend:

        async def contend(worker: int) -> None:
            nonlocal held, max_held
            lock = Lock(
                name="contended",
                backend=backend,
                worker=f"worker-{worker}",
                lease_duration=60,
                retry_interval=0.001,
            )
            async with lock:
                held += 1
                max_held = max(max_held, held)
                # Yield so other tasks get a turn while the lock is held.
                await asyncio.sleep(0)
                held -= 1

        async with asyncio.TaskGroup() as tg:
            for worker in range(CONTENDERS):
                tg.create_task(contend(worker))

    # The lock guarantees mutual exclusion: never more than one holder.
    assert max_held == 1


async def test_backend_startup_shutdown_cycles() -> None:
    """Repeated adapter open/close cycles stay clean and leak no state."""
    for _ in range(STARTUP_CYCLES):
        async with MemoryLockAdapter() as backend:
            lock = Lock(name="cycle", backend=backend, lease_duration=60)
            await lock.acquire()
            assert await lock.locked() is True
        # __aexit__ clears the backend's lock table.
        assert backend._locks == {}
