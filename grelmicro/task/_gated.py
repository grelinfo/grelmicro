"""What a task with a gate, a sync lock or a schedule backend needs.

A task loads this module only when it takes one of those, so a task with
no gate never loads a coordination pattern.
"""

from __future__ import annotations

import string
import warnings
from contextlib import suppress
from hashlib import sha256
from logging import getLogger
from typing import TYPE_CHECKING, Literal

from grelmicro._diagnostics import LEADER_NOT_RUNNING, diagnostic
from grelmicro.clock import monotonic
from grelmicro.coordination._component import Coordination
from grelmicro.coordination._tokens import generate_worker_id
from grelmicro.coordination.errors import CoordinationBackendError
from grelmicro.coordination.leaderelection import LeaderElection, _LeaderGuard
from grelmicro.coordination.lock import LOCK_NAME_MAX_LENGTH, validate_lock_name
from grelmicro.coordination.tasklock import TaskLock, TaskLockConfig
from grelmicro.errors import LeaderNotRunningWarning, SettingsValidationError

if TYPE_CHECKING:
    from datetime import timedelta

    from grelmicro._app import Grelmicro
    from grelmicro.coordination._protocol import LockPrimitive

__all__ = [
    "LeaderWatch",
    "gate_lock_name",
    "has_schedule_backend",
    "interval_gate",
    "label_gate_object",
    "lock_key",
    "refuse_leader_sync",
]

logger = getLogger("grelmicro.task")

_LOCK_KEY_PREFIX = "task-"
_LOCK_KEY_KEPT = frozenset(string.ascii_letters + string.digits + "_.:")
_LOCK_KEY_DIGEST_LEN = 16


def label_gate_object(gate: object, *, takes_lock: bool) -> str:
    """Return how the task logs `gate`, a gate other than `None` or `"claim"`.

    Raises:
        TypeError: If `gate` is not a `LeaderElection`, or a `TaskLock`
            when `takes_lock` is set.
    """
    if isinstance(gate, LeaderElection):
        return f"LeaderElection({gate.name!r})"
    if takes_lock and isinstance(gate, TaskLock):
        return f"TaskLock({gate.name!r})"
    accepted = "'claim', a TaskLock or" if takes_lock else "'claim' or"
    msg = f"gate must be None, {accepted} a LeaderElection, got {gate!r}"
    raise TypeError(msg)


def refuse_leader_sync(sync: LockPrimitive) -> None:
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
    # `micro` exists, so `grelmicro._app` is already loaded.
    from grelmicro._app import ComponentNotRegisteredError  # noqa: PLC0415

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


def lock_key(reference: str) -> str:
    """Return a valid lock name for an auto-derived task reference.

    A reference that is already a valid lock name is returned as is.
    Any other gets a ``task-`` prefix, and every character outside
    ``[A-Za-z0-9_.:]`` becomes ``-{hex code point}-``. A result past 200
    characters keeps its start and ends with ``/`` and a digest of the
    reference. Two ``module:qualname`` references never map to the same
    name.
    """
    with suppress(SettingsValidationError):
        validate_lock_name(reference)
        return reference
    escaped = "".join(
        char if char in _LOCK_KEY_KEPT else f"-{ord(char):x}-"
        for char in reference
    )
    key = _LOCK_KEY_PREFIX + escaped
    if len(key) <= LOCK_NAME_MAX_LENGTH:
        return key
    digest = sha256(reference.encode()).hexdigest()[:_LOCK_KEY_DIGEST_LEN]
    keep = LOCK_NAME_MAX_LENGTH - _LOCK_KEY_DIGEST_LEN - 1
    return f"{key[:keep]}/{digest}"


def gate_lock_name(name: str, *, derived: bool) -> str:
    """Return the lock name a gate takes from a task name.

    A name derived from the function goes through `lock_key`. A name
    passed explicitly is used as is.

    Raises:
        SettingsValidationError: If an explicit name starts with
            ``task-``, the prefix of the names `lock_key` maps.
    """
    if derived:
        return lock_key(name)
    if name.startswith(_LOCK_KEY_PREFIX):
        msg = (
            f"Invalid task name {name!r}. The prefix {_LOCK_KEY_PREFIX!r} is "
            "reserved for the lock names of tasks named after their "
            "function. Pass another name=."
        )
        raise SettingsValidationError(msg)
    return name


def interval_gate(
    task: str,
    gate: Literal["claim"] | TaskLock | LeaderElection,
    interval: timedelta,
    *,
    derived: bool,
) -> tuple[list[LockPrimitive], LeaderWatch | None, TaskLock | None]:
    """Return what an interval task named `task` enters for `gate`.

    Returns the primitives the task enters before the body, in order, the
    watch on a leader election, and the lock that holds the claim. A
    leader guard comes first, because it rejects a worker that is not the
    leader without touching the backend. The claim lock comes next, so
    it is held only once leadership is confirmed.

    Raises:
        TypeError: If `gate` is not `"claim"`, a `TaskLock` or a
            `LeaderElection`.
    """
    if isinstance(gate, TaskLock):
        gate._bind_task(  # noqa: SLF001
            task,
            lock_name=gate.name or gate_lock_name(task, derived=derived),
            interval=interval,
        )
        return [gate], None, gate
    if isinstance(gate, LeaderElection):
        guard = gate.guard()
        claim = _claim_lock(task, interval, derived=derived)
        return [guard, claim], LeaderWatch(gate, task), claim
    if gate != "claim":
        label_gate_object(gate, takes_lock=True)
    claim = _claim_lock(task, interval, derived=derived)
    return [claim], None, claim


def _claim_lock(task: str, interval: timedelta, *, derived: bool) -> TaskLock:
    """Build the lock that holds one claim per interval.

    The claim is held for the whole interval. The task extends it
    while the body runs, so the lease of two intervals only bounds
    how long a crashed worker keeps it.
    The lock is built from a fixed config, so neither the environment
    nor an external reload retunes it.
    """
    lock_name = gate_lock_name(task, derived=derived)
    lock = TaskLock.from_config(
        lock_name,
        TaskLockConfig(
            worker=generate_worker_id(),
            min_hold_duration=interval,
            lease_duration=interval * 2,
        ),
    )
    lock._bind_task(task, lock_name=lock_name, interval=interval)  # noqa: SLF001
    return lock
