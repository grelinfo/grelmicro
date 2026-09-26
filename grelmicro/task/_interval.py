"""Interval Task."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from logging import getLogger
from typing import Any, Literal

from fast_depends import inject

from grelmicro._async import is_async_callable, sleep_or_stop
from grelmicro.coordination._protocol import LockPrimitive
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.coordination.leaderelection import LeaderElection, _LeaderGuard
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import WouldBlockError
from grelmicro.metrics import _emit
from grelmicro.task._cron import FireInfo, FireOutcome, _report_unrun_fire
from grelmicro.task._protocol import Task
from grelmicro.task._utils import validate_and_generate_reference

logger = getLogger("grelmicro.task")


class IntervalTask(Task):
    """Interval Task.

    Use the `Tasks.every()` or `TaskRouter.every()` decorator instead
    of creating IntervalTask objects directly.

    The ``gate`` decides which workers run each interval:

    - ``None``: every worker.
    - ``"claim"``: one worker claims each interval.
    - A ``TaskLock``: one worker claims each interval, with the lock's
      own tuning.
    - A ``LeaderElection``: the elected worker.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        name: str | None = None,
        seconds: float | timedelta,
        gate: Literal["claim"] | TaskLock | LeaderElection | None = None,
        sync: LockPrimitive | None = None,
    ) -> None:
        """Initialize the IntervalTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            ValueError: If seconds is less than or equal to 0.
            ValueError: If the gate lock holds a claim for less than
                `seconds`, or already gates another task.
            TypeError: If `gate` is not a supported value, or `sync` is
                a leader election.
        """
        seconds = (
            seconds.total_seconds()
            if isinstance(seconds, timedelta)
            else seconds
        )
        if seconds <= 0:
            msg = "seconds must be greater than 0"
            raise ValueError(msg)
        _check_sync(sync)

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._seconds = seconds
        self._function = function
        self._async_function = self._prepare_async_function(function)

        self._gate_label, primitives = self._resolve_gate(gate, seconds)
        if sync is not None:
            primitives.append(sync)
        self._sync_primitives: list[LockPrimitive] = primitives

        self._last_fire: FireInfo | None = None
        self._last_loop_start: float | None = None
        # Bound once: the task name never changes, so every emit that
        # carries only the name reuses this mapping instead of building one.
        self._metric_attrs: dict[str, Any] = {"grelmicro.task.name": self._name}
        # Whether the body started on the current iteration. A failure
        # raised after it started is already counted by `_run_with_sync`.
        self._body_started = False

    def _resolve_gate(
        self,
        gate: Literal["claim"] | TaskLock | LeaderElection | None,
        seconds: float,
    ) -> tuple[str, list[LockPrimitive]]:
        """Resolve the gate into its log label and ordered sync primitives.

        A leader guard comes first, because it rejects a worker that is
        not the leader without touching the backend. The claim lock comes
        next, so it is held only once leadership is confirmed.
        """
        if gate is None:
            return "none", []
        if gate == "claim":
            return "claim", [self._claim_lock(seconds)]
        if isinstance(gate, TaskLock):
            _check_task_lock(gate, seconds)
            gate._bind_task(self._name)  # noqa: SLF001
            return f"TaskLock({gate.name!r})", [gate]
        if isinstance(gate, LeaderElection):
            return f"LeaderElection({gate.name!r})", [
                gate.guard(),
                self._claim_lock(seconds),
            ]
        msg = (
            "gate must be None, 'claim', a TaskLock or a LeaderElection,"
            f" got {gate!r}"
        )
        raise TypeError(msg)

    def _claim_lock(self, seconds: float) -> TaskLock:
        """Build the lock that holds one claim per interval.

        The claim is held for the whole interval, and the lease lets a
        body run for up to two intervals before a peer may claim again.
        """
        return TaskLock(
            self._name,
            min_hold_duration=seconds,
            lease_duration=seconds * 2,
            env_load=False,
        )

    @property
    def function(self) -> Callable[..., Any]:
        """The function the task runs, as it was registered.

        `Tasks.add_task` reads it to mark the function, so a decorator
        applied below the one that registered the task is refused
        rather than silently absent from every run.
        """
        return self._function

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    @property
    def next_fire_time(self) -> datetime | None:
        """The computed next fire time based on last loop instant, or None when not started."""
        if self._last_loop_start is None:
            return None
        elapsed = time.monotonic() - self._last_loop_start
        remaining = max(self._seconds - elapsed, 0)
        return datetime.now(UTC) + timedelta(seconds=remaining)

    @property
    def last_fire(self) -> FireInfo | None:
        """The most recent fire info, or None before the first fire."""
        return self._last_fire

    async def __call__(
        self,
        *,
        ready: asyncio.Future[None] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run the repeated task loop."""
        logger.info(
            "Task started (interval: %ss, gate: %s): %s",
            self._seconds,
            self._gate_label,
            self.name,
        )
        if ready is not None and not ready.done():  # pragma: no branch
            ready.set_result(None)
        try:
            while True:
                self._body_started = False
                try:
                    await self._run_with_sync(self._sync_primitives)
                except asyncio.CancelledError:
                    raise
                except WouldBlockError as exc:
                    self._last_fire = _report_unrun_fire(
                        self.name, datetime.now(UTC), FireOutcome.SKIPPED
                    )
                    logger.debug("Task skipped: %s (%s)", self.name, exc)
                except LockNotOwnedError:
                    # The lock expired on release, so the body already ran
                    # and already reported its own outcome. Counting it
                    # again would double the fire.
                    logger.warning(
                        "Task took too long and lock expired: %s."
                        " Consider increasing lease_duration.",
                        self.name,
                    )
                except Exception as exc:
                    logger.exception(
                        "Task synchronization error: %s", self.name
                    )
                    if not self._body_started:
                        self._last_fire = _report_unrun_fire(
                            self.name,
                            datetime.now(UTC),
                            FireOutcome.COORDINATION_ERROR,
                            exc,
                        )
                # Re-raise pending cancellation that an inner cleanup
                # may have shadowed with a regular Exception.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    task.uncancel()
                    raise asyncio.CancelledError
                # The current iteration finished. Break here on a graceful
                # stop so in-flight work is never interrupted; otherwise
                # sleep until the next interval (waking early on stop).
                self._last_loop_start = time.monotonic()
                _emit.observe(
                    "grelmicro.task.next_run",
                    time.time() + self._seconds,
                    self._metric_attrs,
                    unit="s",
                )
                if await sleep_or_stop(self._seconds, stop):
                    break
        finally:
            logger.info("Task stopped: %s", self.name)

    def _record_schedule_delay(self) -> None:
        """Record how late the body started against its planned instant.

        The interval is measured from the end of the previous iteration,
        so the planned instant is that moment plus `seconds`. It rises
        when a worker is saturated or when acquiring the lock takes
        longer than the interval it guards. Nothing was planned before
        the first iteration, which records no point.
        """
        planned = self._last_loop_start
        if planned is None:
            return
        _emit.record_duration(
            "grelmicro.task.schedule.delay",
            max(time.monotonic() - (planned + self._seconds), 0.0),
            self._metric_attrs,
        )

    async def _run_with_sync(
        self, primitives: list[LockPrimitive], index: int = 0
    ) -> None:
        """Enter sync primitives via recursive nesting, then run the task.

        Using explicit recursion avoids AsyncExitStack and @asynccontextmanager
        overhead on every iteration.
        """
        if index >= len(primitives):
            self._body_started = True
            _emit.add_up_down(
                "grelmicro.task.active", 1, self._metric_attrs, unit="{run}"
            )
            started_at = datetime.now(UTC)
            self._record_schedule_delay()
            start_monotonic = time.perf_counter()
            outcome = FireOutcome.ERROR
            # Assumes failure until the body returns, so a fire cancelled
            # mid-body records its duration as the error it was.
            attributes: dict[str, Any] = {
                "grelmicro.task.name": self._name,
                "grelmicro.outcome": FireOutcome.ERROR,
            }
            try:
                await self._async_function()
                outcome = FireOutcome.SUCCESS
                attributes["grelmicro.outcome"] = FireOutcome.SUCCESS
                _emit.incr("grelmicro.task.runs", attributes, unit="{run}")
            except Exception as exc:
                logger.exception("Task execution error: %s", self.name)
                attributes["error.type"] = type(exc).__name__
                _emit.incr("grelmicro.task.runs", attributes, unit="{run}")
            finally:
                duration = time.perf_counter() - start_monotonic
                self._last_fire = FireInfo(
                    started_at=started_at,
                    outcome=outcome,
                    duration=duration,
                )
                _emit.record_duration(
                    "grelmicro.task.duration", duration, attributes
                )
                _emit.add_up_down(
                    "grelmicro.task.active",
                    -1,
                    self._metric_attrs,
                    unit="{run}",
                )
            return

        async with primitives[index]:
            await self._run_with_sync(primitives, index + 1)

    def _prepare_async_function(
        self, function: Callable[..., Any]
    ) -> Callable[..., Awaitable[Any]]:
        """Prepare the function with lock and ensure async function."""
        function = inject(function)
        return (
            function
            if is_async_callable(function)
            else partial(asyncio.to_thread, function)
        )


def _check_task_lock(lock: TaskLock, seconds: float) -> None:
    """Refuse a lock that cannot hold one claim for a whole interval.

    `TaskLockConfig` keeps `lease_duration` at or above
    `min_hold_duration`, so the lease covers the interval too.

    Raises:
        ValueError: If `min_hold_duration` is shorter than `seconds`.
    """
    if lock.config.min_hold_duration < seconds:
        msg = (
            "min_hold_duration must be greater than or equal to seconds,"
            " or a peer claims the same interval once the body ends"
        )
        raise ValueError(msg)


def _check_sync(sync: LockPrimitive | None) -> None:
    """Refuse a leader election passed as `sync`.

    Raises:
        TypeError: If `sync` is a `LeaderElection` or its guard.
    """
    if isinstance(sync, LeaderElection | _LeaderGuard):
        msg = "sync takes a resource lock, pass a LeaderElection as gate="
        raise TypeError(msg)
