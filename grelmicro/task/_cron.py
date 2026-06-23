"""Cron Task."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from logging import getLogger
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fast_depends import inject

from grelmicro._async import is_async_callable, sleep_or_stop
from grelmicro.coordination.errors import LockNotOwnedError
from grelmicro.errors import WouldBlockError
from grelmicro.metrics import _emit
from grelmicro.task._utils import validate_and_generate_reference
from grelmicro.task.abc import Task
from grelmicro.task.errors import CronError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from grelmicro.coordination.abc import LockPrimitive, ScheduleBackend

logger = getLogger("grelmicro.task")


class FireOutcome(StrEnum):
    """Outcome of a task fire.

    - ``SUCCESS``: the body ran and returned without raising.
    - ``ERROR``: the body raised an exception.
    - ``SKIPPED``: the fire was skipped because acquiring a lock would
      block (a ``WouldBlockError``).
    """

    SUCCESS = "success"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class FireInfo:
    """Information about a task fire."""

    started_at: datetime
    outcome: FireOutcome
    duration: float


# Wall-clock seam for the current time. Tests pin it to a fixed instant so
# cron matching is deterministic and never straddles a real minute boundary.
# Mirrors the `_now` seam in the cache.
_now = datetime.now

# Maximum number of years to search for the next matching datetime before
# giving up. Covers Feb-29 schedules (every 4 years) with margin so an
# impossible date such as ``30 2 31 2 *`` (Feb 31) raises instead of looping.
_MAX_SEARCH_YEARS = 5

_FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")
_FIELD_COUNT = 5
_SUNDAY_ALIAS = 7
_DECEMBER = 12


def _parse_step(step_spec: str, name: str, part: str) -> int:
    """Parse the step component after ``/``.

    Raises:
        CronError: If the step is missing, non-numeric, or not positive.
    """
    if not step_spec:
        reason = f"missing step in {name} field: {part!r}"
        raise CronError(reason)
    try:
        step = int(step_spec)
    except ValueError:
        reason = f"invalid step in {name} field: {part!r}"
        raise CronError(reason) from None
    if step <= 0:
        reason = f"step must be positive in {name} field: {part!r}"
        raise CronError(reason)
    return step


def _parse_bounds(
    range_spec: str,
    low: int,
    high: int,
    name: str,
    part: str,
    *,
    stepped: bool,
) -> tuple[int, int]:
    """Parse the start and end bounds of a single part.

    Raises:
        CronError: If the bounds are malformed.
    """
    if range_spec == "*":
        return low, high
    if "-" in range_spec:
        start_spec, _, end_spec = range_spec.partition("-")
        try:
            start, end = int(start_spec), int(end_spec)
        except ValueError:
            reason = f"invalid range in {name} field: {part!r}"
            raise CronError(reason) from None
        if start > end:
            reason = f"range start after end in {name} field: {part!r}"
            raise CronError(reason)
        return start, end
    if stepped:
        reason = f"step requires '*' or a range in {name} field: {part!r}"
        raise CronError(reason)
    try:
        value = int(range_spec)
    except ValueError:
        reason = f"invalid value in {name} field: {part!r}"
        raise CronError(reason) from None
    return value, value


def _parse_field(field: str, low: int, high: int, name: str) -> frozenset[int]:
    """Parse a single cron field into the set of matching integers.

    Supports ``*``, ``*/step``, ``a-b``, ``a-b/step``, a bare integer, and a
    comma list of any of the above.

    Raises:
        CronError: If the field is malformed or out of range.
    """
    values: set[int] = set()
    for part in field.split(","):
        if not part:
            reason = f"empty value in {name} field"
            raise CronError(reason)
        range_spec, sep, step_spec = part.partition("/")
        step = _parse_step(step_spec, name, part) if sep else 1
        start, end = _parse_bounds(
            range_spec, low, high, name, part, stepped=bool(sep)
        )
        if start < low or end > high:
            reason = (
                f"value out of range [{low}-{high}] in {name} field: {part!r}"
            )
            raise CronError(reason)
        values.update(range(start, end + 1, step))

    return frozenset(values)


class CronExpression:
    """Parsed 5-field cron expression.

    Fields are ``minute hour day-of-month month day-of-week``. Day of week
    uses 0-6 with 0 = Sunday, and accepts 7 as an alias for Sunday.

    Day-of-month and day-of-week follow standard Vixie cron semantics: when
    both are restricted (neither is ``*``), a day matches if it matches
    EITHER field. When only one is restricted, only that one applies.
    """

    def __init__(self, expr: str) -> None:
        """Parse the cron expression.

        Raises:
            CronError: If the expression is malformed.
        """
        self._expr = expr
        fields = expr.split()
        if len(fields) != _FIELD_COUNT:
            reason = f"expected 5 fields, got {len(fields)}: {expr!r}"
            raise CronError(reason)

        self._minutes = _parse_field(fields[0], 0, 59, _FIELD_NAMES[0])
        self._hours = _parse_field(fields[1], 0, 23, _FIELD_NAMES[1])
        self._days = _parse_field(fields[2], 1, 31, _FIELD_NAMES[2])
        self._months = _parse_field(fields[3], 1, 12, _FIELD_NAMES[3])
        # Parse day of week against 0-7, then normalize 7 to 0 (Sunday).
        dow = _parse_field(fields[4], 0, _SUNDAY_ALIAS, _FIELD_NAMES[4])
        self._weekdays = frozenset(0 if d == _SUNDAY_ALIAS else d for d in dow)

        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"

    def __repr__(self) -> str:
        """Return the source expression."""
        return f"CronExpression({self._expr!r})"

    def _day_matches(self, candidate: datetime) -> bool:
        """Return whether the candidate's day matches dom and dow rules."""
        # Python weekday(): Monday = 0 .. Sunday = 6. Convert to cron's
        # Sunday = 0 .. Saturday = 6.
        cron_dow = (candidate.weekday() + 1) % 7
        dom_match = candidate.day in self._days
        dow_match = cron_dow in self._weekdays
        if self._dom_restricted and self._dow_restricted:
            return dom_match or dow_match
        if self._dom_restricted:
            return dom_match
        if self._dow_restricted:
            return dow_match
        return True

    def next_after(self, dt: datetime) -> datetime:
        """Return the next datetime strictly after ``dt`` that matches.

        The result is timezone-aware with the same ``tzinfo`` as ``dt`` and
        truncated to whole minutes.

        Raises:
            CronError: If no matching datetime is found within five years
                (an impossible schedule such as ``30 2 31 2 *``).
        """
        # Start at the next whole minute strictly after dt.
        candidate = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        # A timedelta bound, not `replace(year=...)`, so a Feb 29 candidate
        # never lands on a non-leap year and raises.
        limit = candidate + timedelta(days=_MAX_SEARCH_YEARS * 366)

        while candidate <= limit:
            if candidate.month not in self._months:
                # Advance to the first day of the next valid month.
                candidate = self._advance_month(candidate)
                continue
            if not self._day_matches(candidate):
                candidate = candidate.replace(hour=0, minute=0) + timedelta(
                    days=1
                )
                continue
            if candidate.hour not in self._hours:
                candidate = candidate.replace(minute=0) + timedelta(hours=1)
                continue
            if candidate.minute not in self._minutes:
                candidate = candidate + timedelta(minutes=1)
                continue
            return candidate

        reason = (
            f"no matching time within {_MAX_SEARCH_YEARS} years for "
            f"{self._expr!r}"
        )
        raise CronError(reason)

    def previous_or_equal(self, dt: datetime) -> datetime | None:
        """Return the most recent matching datetime at or before ``dt``.

        The result is timezone-aware with the same ``tzinfo`` as ``dt`` and
        truncated to whole minutes. Returns ``None`` when no match falls
        within five years before ``dt``.
        """
        candidate = dt.replace(second=0, microsecond=0)
        # A timedelta bound, not `replace(year=...)`, so a Feb 29 candidate
        # never lands on a non-leap year and raises.
        limit = candidate - timedelta(days=_MAX_SEARCH_YEARS * 366)

        while candidate >= limit:
            if candidate.month not in self._months:
                candidate = self._retreat_month(candidate)
                continue
            if not self._day_matches(candidate):
                candidate = candidate.replace(hour=23, minute=59) - timedelta(
                    days=1
                )
                continue
            if candidate.hour not in self._hours:
                candidate = candidate.replace(minute=59) - timedelta(hours=1)
                continue
            if candidate.minute not in self._minutes:
                candidate = candidate - timedelta(minutes=1)
                continue
            return candidate

        return None

    def _advance_month(self, candidate: datetime) -> datetime:
        """Return midnight on the first day of the next month."""
        if candidate.month == _DECEMBER:
            return candidate.replace(
                year=candidate.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
            )
        return candidate.replace(
            month=candidate.month + 1, day=1, hour=0, minute=0
        )

    def _retreat_month(self, candidate: datetime) -> datetime:
        """Return 23:59 on the last day of the previous month."""
        first_of_month = candidate.replace(day=1, hour=0, minute=0)
        return first_of_month - timedelta(minutes=1)


class CronTask(Task):
    """Cron Task.

    Use the `Tasks.cron()` or `TaskRouter.cron()` decorator instead of
    creating CronTask objects directly.

    Each tick computes the most recent scheduled fire at or before now and
    asks the schedule backend to claim it. Exactly one worker wins the claim
    and runs the body, so a fire runs at most once across every worker. The
    backend stores the last fire durably, so a fire missed while every worker
    was down replays once on restart, bounded by ``misfire_grace_seconds``.
    Only the most recent missed fire runs, never a backlog.

    Without a schedule backend the task runs on every worker, every fire.
    """

    def __init__(
        self,
        *,
        function: Callable[..., Any],
        expr: str,
        timezone: str = "UTC",
        name: str | None = None,
        misfire_grace_seconds: float | None = None,
        backend: ScheduleBackend | None = None,
        sync: LockPrimitive | None = None,
    ) -> None:
        """Initialize the CronTask.

        Raises:
            FunctionTypeError: If the function is not supported.
            CronError: If the cron expression is invalid.
        """
        self._expr = CronExpression(expr)
        self._expr_source = expr
        self._tz = ZoneInfo(timezone)
        self._timezone = timezone

        alt_name = validate_and_generate_reference(function)
        self._name = name or alt_name
        self._async_function = self._prepare_async_function(function)

        self._misfire_grace_seconds = misfire_grace_seconds
        self._backend = backend
        self._sync = sync

        self._next_fire_time: datetime | None = None
        self._last_fire: FireInfo | None = None

    @property
    def name(self) -> str:
        """Return the task name."""
        return self._name

    @property
    def next_fire_time(self) -> datetime | None:
        """The next scheduled fire time, or None when not started."""
        return self._next_fire_time

    @property
    def last_fire(self) -> FireInfo | None:
        """The most recent fire info, or None before the first fire."""
        return self._last_fire

    @property
    def backend(self) -> ScheduleBackend | None:
        """Bound schedule backend, resolved on each tick.

        When a backend instance was passed at construction it is always
        returned. Otherwise the active `Grelmicro` app is consulted via
        `Grelmicro.current()` so that `micro.override(Coordination(...))`
        blocks take effect. Returns `None` when no app is running and no
        backend was passed, which runs the body on every worker.

        Raises:
            OutOfContextError: An app is running but no `Coordination`
                component is registered. Pass `backend=`, register a
                `Coordination` Component, or run the call under the app
                context (for FastAPI, add `GrelmicroMiddleware`).
        """
        if self._backend is not None:
            return self._backend
        from grelmicro._app import (  # noqa: PLC0415
            ComponentNotRegisteredError,
            Grelmicro,
            NoActiveAppError,
        )
        from grelmicro.errors import OutOfContextError  # noqa: PLC0415

        try:
            app = Grelmicro.current()
        except NoActiveAppError:
            return None
        try:
            coordination = app.get("coordination", "default")
        except ComponentNotRegisteredError:
            msg = (
                f"Cron task {self.name!r} resolved no schedule backend. "
                f"Pass backend=, register a Coordination component, or run "
                f"the call under the app context (for FastAPI add "
                f"GrelmicroMiddleware)."
            )
            raise OutOfContextError(msg) from None
        return coordination.schedule_backend

    async def __call__(
        self,
        *,
        ready: asyncio.Future[None] | None = None,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run the cron task loop."""
        logger.info(
            "Task started (cron: %s, timezone: %s): %s",
            self._expr_source,
            self._timezone,
            self.name,
        )
        if ready is not None and not ready.done():  # pragma: no branch
            ready.set_result(None)
        try:
            # Replay a fire missed while this worker was down before sleeping
            # to the next one. Only meaningful with a durable backend: in local
            # mode there is no past state, so startup is the baseline.
            await self._tick_guarded(catchup=True)
            while True:
                now = _now(self._tz)
                next_fire = self._expr.next_after(now)
                self._next_fire_time = next_fire
                delay = next_fire.timestamp() - now.timestamp()
                # Wait until the next fire instant, waking early on stop.
                if await sleep_or_stop(delay, stop):
                    break
                await self._tick_guarded(catchup=False)
        finally:
            logger.info("Task stopped: %s", self.name)

    async def _tick_guarded(self, *, catchup: bool) -> None:
        """Run one tick, catching the errors a single fire may raise."""
        try:
            await self._tick(catchup=catchup)
        except asyncio.CancelledError:
            raise
        except WouldBlockError as exc:
            self._last_fire = FireInfo(
                started_at=_now(self._tz),
                outcome=FireOutcome.SKIPPED,
                duration=0.0,
            )
            logger.debug("Task skipped: %s (%s)", self.name, exc)
        except LockNotOwnedError:
            logger.warning(
                "Task took too long and lock expired: %s.", self.name
            )
        except Exception:
            logger.exception("Task synchronization error: %s", self.name)
        # Re-raise pending cancellation that an inner cleanup may have
        # shadowed with a regular Exception.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            task.uncancel()
            raise asyncio.CancelledError

    async def _tick(self, *, catchup: bool) -> None:
        """Evaluate the current fire and run the body when this worker claims it.

        Computes ``due``, the most recent scheduled fire at or before now. With
        no backend, runs the body for a scheduled fire (every worker) and skips
        the startup catch-up tick. With a backend, claims ``due`` against the
        durable ``last_fired`` and runs only on a won claim, dropping coalesced
        or out-of-grace fires.
        """
        now = _now(self._tz)
        due_dt = self._expr.previous_or_equal(now)
        if due_dt is None:  # pragma: no cover - schedule always has a past fire
            return
        due = due_dt.timestamp()

        backend = self.backend
        if backend is None:
            # Local mode: no durable state, so a past fire cannot be replayed.
            # Skip the startup catch-up and run the body for scheduled fires.
            if not catchup:
                await self._run()
            return

        last = await backend.last_fired(self.name)
        if last is None:
            # First sight of this schedule: establish the baseline without
            # running, so only later fires count as missed and replay.
            await backend.claim(self.name, due)
            return
        if due <= last:
            # Already handled (coalesce: only the most recent fire matters).
            return
        if (
            self._misfire_grace_seconds is not None
            and (now.timestamp() - due) > self._misfire_grace_seconds
        ):
            # Too late to replay this missed fire: skip but advance the
            # baseline so it is not retried forever.
            await backend.claim(self.name, due)
            return
        if await backend.claim(self.name, due):
            await self._run()

    async def _run(self) -> None:
        """Run the body, optionally under the resource sync lock, with metrics."""
        if self._sync is not None:
            async with self._sync:
                await self._run_body()
        else:
            await self._run_body()

    async def _run_body(self) -> None:
        """Run the task body and emit metrics."""
        _emit.add_up_down(
            "grelmicro.task.active", 1, **{"task.name": self.name}
        )
        started_at = _now(self._tz)
        start_monotonic = time.perf_counter()
        outcome = FireOutcome.ERROR
        try:
            await self._async_function()
            outcome = FireOutcome.SUCCESS
            _emit.incr(
                "grelmicro.task.runs",
                **{"task.name": self.name, "outcome": FireOutcome.SUCCESS},
            )
        except Exception as exc:
            logger.exception("Task execution error: %s", self.name)
            _emit.incr(
                "grelmicro.task.runs",
                **{
                    "task.name": self.name,
                    "outcome": FireOutcome.ERROR,
                    "error.type": type(exc).__name__,
                },
            )
        finally:
            duration = time.perf_counter() - start_monotonic
            self._last_fire = FireInfo(
                started_at=started_at,
                outcome=outcome,
                duration=duration,
            )
            _emit.record_duration(
                "grelmicro.task.duration",
                duration,
                **{"task.name": self.name},
            )
            _emit.add_up_down(
                "grelmicro.task.active", -1, **{"task.name": self.name}
            )

    def _prepare_async_function(
        self, function: Callable[..., Any]
    ) -> Callable[..., Awaitable[Any]]:
        """Prepare the function and ensure it is async."""
        function = inject(function)
        return (
            function
            if is_async_callable(function)
            else partial(asyncio.to_thread, function)
        )
