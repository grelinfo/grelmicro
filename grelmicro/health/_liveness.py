"""Liveness: let a stuck worker exit, so whatever runs it replaces it."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
import traceback
from logging import getLogger
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, PositiveFloat, PositiveInt
from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Callable

logger = getLogger("grelmicro.health")

_EXIT_CODE = 70
"""The exit code of a worker whose event loop stalled (`EX_SOFTWARE`)."""


class Liveness(BaseModel, frozen=True, extra="forbid"):
    """When a worker counts as stuck, and exits so it is replaced.

    A stuck worker exits, and the process manager that runs it starts
    another: the uvicorn supervisor, the Gunicorn arbiter, or Kubernetes for
    a container whose only process ends. A loop that stalls past
    `stall_timeout` exits at once. Liveness checks that fail
    `failure_threshold` rounds in a row stop the worker with `SIGTERM`, and
    `/livez` answers `503` from the first failed round.
    """

    stall_timeout: Annotated[
        PositiveFloat | None,
        Doc(
            "Seconds the event loop may go without running a callback. A "
            "watchdog thread checks four times per `stall_timeout`, at most "
            "once a second. Past it, the watchdog logs the loop thread's "
            "stack and exits the process. `None` (the default) starts no "
            "watchdog."
        ),
    ] = None
    interval: Annotated[
        PositiveFloat,
        Doc("Seconds between two runs of the liveness checks."),
    ] = 10.0
    failure_threshold: Annotated[
        PositiveInt,
        Doc(
            "Liveness rounds that must fail in a row before the worker stops "
            "itself with `SIGTERM`. `/livez` answers `503` from the first one."
        ),
    ] = 3
    shutdown_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Seconds a worker stopped by failed liveness checks has to shut "
            "down. Past it, the worker exits at once, as it does when it runs "
            "as PID 1 with no `SIGTERM` handler or its shutdown hangs."
        ),
    ] = 30.0


def _stop_process() -> None:
    """Stop this worker the way its process manager would, with `SIGTERM`."""
    os.kill(os.getpid(), signal.SIGTERM)


def _exit_process() -> None:
    """Exit at once, since a stalled loop cannot run a graceful shutdown."""
    os._exit(_EXIT_CODE)


def _exit_after(seconds: float) -> None:
    """Exit at once after `seconds`, unless the process ended before."""
    timer = threading.Timer(seconds, _exit_process)
    timer.daemon = True
    timer.start()


class Watchdog:
    """A thread that exits the process when the event loop stops running.

    It posts a callback to the loop every tick, a quarter of
    `stall_timeout` capped at one second, and exits once none has run for
    `stall_timeout` seconds. The loop thread's stack is logged first, so the
    log says where the loop was stuck. It pauses while `watching` reads
    false or the loop is not running, stopped between two runs, and resumes
    when both change back.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        stall_timeout: float,
        watching: Callable[[], bool] = lambda: True,
    ) -> None:
        """Watch `loop`, from the thread that runs it, while `watching()`."""
        self._loop = loop
        self._stall_timeout = stall_timeout
        self._watching = watching
        self._tick = min(1.0, stall_timeout / 4)
        self._loop_thread = threading.get_ident()
        self._last_beat = time.monotonic()
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name="grelmicro-liveness-watchdog", daemon=True
        )

    def start(self) -> None:
        """Start watching."""
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and wait for the thread to end."""
        self._stopped.set()
        self._thread.join()

    def _beat(self) -> None:
        self._last_beat = time.monotonic()

    def _watch(self) -> None:
        while not self._stopped.wait(self._tick):
            if not self._watching() or not self._loop.is_running():
                self._last_beat = time.monotonic()
                continue
            try:
                self._loop.call_soon_threadsafe(self._beat)
            except RuntimeError:
                return
            stalled = time.monotonic() - self._last_beat
            if stalled > self._stall_timeout:
                self._report(stalled)
                _exit_process()
                return

    def _report(self, stalled: float) -> None:
        frame = sys._current_frames().get(self._loop_thread)  # noqa: SLF001
        stack = (
            "".join(traceback.format_stack(frame)) if frame is not None else ""
        )
        logger.critical(
            "Event loop stalled for %.1fs, past stall_timeout %.1fs. Exiting "
            "so the worker is replaced. The loop thread was at:\n%s",
            stalled,
            self._stall_timeout,
            stack,
        )
