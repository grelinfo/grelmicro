"""Which workers run a scheduled task, checked the same way for every kind."""

from __future__ import annotations

import inspect
import warnings
from contextlib import suppress
from logging import getLogger
from types import FunctionType
from typing import TYPE_CHECKING

from grelmicro._app import ComponentNotRegisteredError, _current_micro
from grelmicro._diagnostics import LEADER_NOT_RUNNING, UNGATED_CRON, diagnostic
from grelmicro._environment import _PACKAGE_DIR, QUIET_ENVIRONMENTS
from grelmicro.clock import monotonic
from grelmicro.coordination._component import Coordination
from grelmicro.coordination.errors import CoordinationBackendError
from grelmicro.coordination.leaderelection import LeaderElection, _LeaderGuard
from grelmicro.coordination.tasklock import TaskLock
from grelmicro.errors import LeaderNotRunningWarning, UngatedCronWarning

if TYPE_CHECKING:
    from collections.abc import Callable

    from grelmicro._app import Grelmicro
    from grelmicro.coordination._protocol import LockPrimitive

__all__ = [
    "LeaderWatch",
    "check_sync",
    "gate_label",
    "has_schedule_backend",
    "report_ungated_cron",
    "ungated_cron_finding",
]

logger = getLogger("grelmicro.task")


def gate_label(gate: object, *, takes_lock: bool) -> str:
    """Return how the task logs `gate`, once it is one the task takes.

    Every kind of task takes `None`, `"claim"` and a `LeaderElection`. A
    `TaskLock` is taken only when `takes_lock` is set.

    Raises:
        TypeError: If `gate` is none of those.
    """
    if gate is None:
        return "none"
    if gate == "claim":
        return "claim"
    if isinstance(gate, LeaderElection):
        return f"LeaderElection({gate.name!r})"
    if takes_lock and isinstance(gate, TaskLock):
        return f"TaskLock({gate.name!r})"
    accepted = "'claim', a TaskLock or" if takes_lock else "'claim' or"
    msg = f"gate must be None, {accepted} a LeaderElection, got {gate!r}"
    raise TypeError(msg)


def check_sync(sync: LockPrimitive | None) -> None:
    """Refuse a leader election passed as `sync`.

    Raises:
        TypeError: If `sync` is a `LeaderElection` or its guard.
    """
    if isinstance(sync, LeaderElection | _LeaderGuard):
        msg = "sync takes a resource lock, pass a LeaderElection as gate="
        raise TypeError(msg)


class LeaderWatch:
    """Report a task gated on a leader election that does not run.

    An election acquires leadership only while it runs, so the task it
    gates skips every fire. The watch reports it once, on a fire at least
    one lease duration after the task started, when the election has not
    run at any fire it saw. An election that runs at a fire settles the
    watch, and so does the report.
    """

    __slots__ = ("_election", "_settled", "_since", "_task")

    def __init__(self, election: LeaderElection, task: str) -> None:
        """Watch `election` for the task named `task`."""
        self._election = election
        self._task = task
        self._since: float | None = None
        self._settled = False

    def start(self) -> None:
        """Mark the moment the task started."""
        self._since = monotonic()

    def check(self) -> None:
        """Report the election once when it has not run for a lease duration.

        The report is a log record on the `grelmicro.task` logger and a
        `LeaderNotRunningWarning`, both carrying the `leader-not-running`
        code. A warnings filter set to `error` never raises it into the
        task.
        """
        since = self._since
        if self._settled or since is None:
            return
        election = self._election
        if election.is_running():
            self._settled = True
            return
        if monotonic() - since < election.config.lease_duration.total_seconds():
            return
        self._settled = True
        msg = diagnostic(
            LEADER_NOT_RUNNING,
            f"Task {self._task!r} is gated on the leader election "
            f"{election.name!r}, which has not run since the task started, "
            f"so the task skips every fire. Register it with "
            f"tasks.add_task(election), or start it.",
        )
        logger.warning(msg, extra={"diagnostic": LEADER_NOT_RUNNING})
        # A filter that turns the warning into an error must not end the
        # task loop. The log record above still carries the report.
        with suppress(LeaderNotRunningWarning):
            warnings.warn(msg, LeaderNotRunningWarning, stacklevel=2)


def has_schedule_backend(micro: Grelmicro) -> bool:
    """Return whether `micro` resolves a `Coordination` with a schedule backend."""
    try:
        coordination = micro.get("coordination")
    except ComponentNotRegisteredError:
        return False
    if not isinstance(coordination, Coordination):
        return False
    try:
        _ = coordination.schedule_backend
    except CoordinationBackendError:
        return False
    return True


def ungated_cron_finding(task: str) -> str:
    """Return what a cron task named `task` with no `gate=` does, and the fix."""
    return (
        f"Cron task {task!r} has no gate, so it runs each fire on every "
        f"worker, although a schedule backend is registered. Pass "
        f"gate='claim' to run each fire on one worker, or gate=None to "
        f"keep every worker."
    )


def report_ungated_cron(task: str, function: Callable[..., object]) -> None:
    """Report a cron task named `task`, running `function`, that left `gate=` out.

    Reported each time the task starts inside an open app with a
    schedule backend, and quiet in `development` and `test`. The report
    is a log record on the `grelmicro.task` logger and an
    `UngatedCronWarning`, both carrying the `ungated-cron` code. The
    warning points at the definition of `function`, past any
    `functools.wraps` decorator, so a filter on its module matches it.
    A warnings filter set to `error` never raises it into the task.
    """
    micro = _current_micro.get(None)
    if (
        micro is None
        or micro.environment in QUIET_ENVIRONMENTS
        or not has_schedule_backend(micro)
    ):
        return
    msg = diagnostic(UNGATED_CRON, ungated_cron_finding(task))
    logger.warning(msg, extra={"diagnostic": UNGATED_CRON})
    with suppress(UngatedCronWarning):
        _warn_at_definition(msg, function)


def _warn_at_definition(msg: str, function: Callable[..., object]) -> None:
    """Warn `msg` as an `UngatedCronWarning` at the definition of `function`.

    A callable with no code of its own, such as a callable object, warns
    at the first frame outside grelmicro instead.
    """
    target = inspect.unwrap(function)
    if not isinstance(target, FunctionType):
        warnings.warn(
            msg, UngatedCronWarning, skip_file_prefixes=(_PACKAGE_DIR,)
        )
        return
    module_globals = target.__globals__
    warnings.warn_explicit(
        msg,
        UngatedCronWarning,
        filename=target.__code__.co_filename,
        lineno=target.__code__.co_firstlineno,
        module=target.__module__,
        registry=module_globals.setdefault("__warningregistry__", {}),
        module_globals=module_globals,
    )
