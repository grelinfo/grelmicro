"""Run N worker processes against one backend and collect what they did.

A server runs the app in several processes. `uvicorn --workers N` starts
each one with the `spawn` start method, so a worker imports the
application itself and shares nothing. `gunicorn --preload` loads the
application once and forks, so a worker inherits every module-level
object the parent built. The two models break different things, so both
start methods are drivable from here.

Every worker is a module-level function, because `spawn` resolves it by
qualified name in the child. Workers are released together by a barrier
so they contend, rather than running one after another, and the parent
joins them all before asserting so a straggler cannot land after the
check.
"""

from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.managers import ListProxy

    Results = ListProxy[Any]
    """What a worker appends its observation to, shared with the parent."""

DEFAULT_TIMEOUT = 30.0
"""Seconds the parent waits for a worker before failing the test."""


class WorkerFailedError(AssertionError):
    """Raised when a worker process crashed or did not finish in time."""


def run_workers(
    worker: Callable[..., None],
    count: int,
    *args: Any,  # noqa: ANN401
    start_method: str = "spawn",
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Any]:
    """Run `count` copies of `worker` at once and return what they reported.

    Each worker is called as `worker(barrier, results, *args)`. It should
    call `barrier.wait()` when it is ready and append what it observed to
    `results`. Results come back in completion order, not worker order.

    Raises:
        WorkerFailedError: If a worker exited non-zero or outran `timeout`.
    """
    context = multiprocessing.get_context(start_method)
    barrier = context.Barrier(count)
    results = context.Manager().list()
    processes = [
        # Typeshed types `get_context` as the abstract base, which does not
        # carry `Process`, but every concrete start-method context does.
        context.Process(target=worker, args=(barrier, results, *args))  # ty: ignore[unresolved-attribute]
        for _ in range(count)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout)
        stragglers = [p for p in processes if p.is_alive()]
        if stragglers:
            msg = (
                f"{len(stragglers)} of {count} workers did not finish "
                f"within {timeout}s"
            )
            raise WorkerFailedError(msg)
        failed = [p.exitcode for p in processes if p.exitcode]
        if failed:
            msg = f"workers exited with {failed}"
            raise WorkerFailedError(msg)
    finally:
        for process in processes:
            if process.is_alive():  # pragma: no cover
                process.kill()
                process.join(timeout)
    return list(results)
