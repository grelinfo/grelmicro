"""Bridge for sync code in a worker thread to call into the event loop.

The replacement for ``anyio.from_thread.run`` after the asyncio
migration in 0.21.0. Sync code spawned via
``grelmicro.to_thread.run_sync`` (or any worker thread that has the
parent loop reachable) can use ``run`` to schedule a coroutine on
the captured loop and wait for its result.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

# Set by ``grelmicro.to_thread.run_sync`` and refreshed by every
# grelmicro async API entry. ``asyncio.to_thread`` copies the calling
# task's context to the worker thread, so the worker can read this
# contextvar to find the parent loop.
_loop_var: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "grelmicro_running_loop", default=None
)


def capture_running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the currently running event loop, or ``None`` if not in one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def remember_running_loop() -> asyncio.AbstractEventLoop | None:
    """Capture the running loop into the contextvar and return it.

    Called from grelmicro async APIs so that any subsequent worker
    thread (spawned via ``grelmicro.to_thread.run_sync`` or via
    ``asyncio.to_thread`` in the same task) can reach the loop.
    """
    loop = capture_running_loop()
    if loop is not None:
        _loop_var.set(loop)
    return loop


def run[T](
    loop: asyncio.AbstractEventLoop | None,
    func: Callable[..., Coroutine[Any, Any, T]],
    *args: Any,  # noqa: ANN401
) -> T:
    """Run ``func(*args)`` on the parent event loop from a worker thread.

    Resolution order for the loop:

    1. The explicit ``loop`` argument when not ``None`` and not closed.
    2. The contextvar set by ``grelmicro.to_thread.run_sync`` or by
       any grelmicro async API the calling task touched.
    """
    if loop is None or loop.is_closed():
        loop = _loop_var.get()
    if loop is None or loop.is_closed():
        msg = (
            "from_thread requires a captured event loop. Spawn the "
            "worker via grelmicro.to_thread.run_sync, or call any "
            "grelmicro async API on the lock from the parent task "
            "first."
        )
        raise RuntimeError(msg)
    future = asyncio.run_coroutine_threadsafe(func(*args), loop)
    return future.result()
