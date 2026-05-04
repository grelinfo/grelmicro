"""Bridge for sync code in a worker thread to call back into the event loop.

Replaces ``anyio.from_thread.run``. Resolves the parent loop in two
ways:

1. The grelmicro primitive (``Lock``, ``TaskLock``, ``TTLCache``,
   ``CircuitBreaker``) captures its loop on construction or on the
   first async call and passes it explicitly to :func:`run`.
2. ``grelmicro.to_thread.run_sync`` records the running loop in a
   contextvar. ``asyncio.to_thread`` copies the calling task's context
   to the worker thread, so the worker can read the contextvar to
   reach the loop even when the primitive itself was constructed
   outside of any loop.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

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

    ``grelmicro.to_thread.run_sync`` calls this so a worker thread
    spawned via ``asyncio.to_thread`` can reach the parent loop.
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
    2. The contextvar set by ``grelmicro.to_thread.run_sync``.
    """
    if loop is None or loop.is_closed():
        loop = _loop_var.get()
    if loop is None or loop.is_closed():
        msg = (
            "from_thread requires a captured event loop. Spawn the "
            "worker via grelmicro.to_thread.run_sync, or touch the "
            "primitive from an async task first."
        )
        raise RuntimeError(msg)
    future = asyncio.run_coroutine_threadsafe(func(*args), loop)
    return future.result()
