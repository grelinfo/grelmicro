"""Which workers run a scheduled task, checked the same way for every kind."""

from __future__ import annotations

import inspect
import warnings
from contextlib import suppress
from logging import getLogger
from types import FunctionType
from typing import TYPE_CHECKING, Literal

from grelmicro._config import match_case
from grelmicro._current_app import _current_micro
from grelmicro._diagnostics import UNGATED_CRON, diagnostic
from grelmicro._environment import _PACKAGE_DIR, QUIET_ENVIRONMENTS
from grelmicro.errors import UngatedCronWarning

if TYPE_CHECKING:
    from collections.abc import Callable

    from grelmicro.coordination._protocol import LockPrimitive

__all__ = [
    "check_sync",
    "gate_label",
    "report_ungated_cron",
    "ungated_cron_finding",
]

logger = getLogger("grelmicro.task")


def spell_gate[T](gate: T) -> T:
    """Return `gate` with `"claim"` matched in any case, anything else as is."""
    return match_case(Literal["claim"], gate)


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
    from grelmicro.task import _gated  # noqa: PLC0415

    return _gated.label_gate_object(gate, takes_lock=takes_lock)


def check_sync(sync: LockPrimitive | None) -> None:
    """Refuse a leader election passed as `sync`.

    Raises:
        TypeError: If `sync` is a `LeaderElection` or its guard.
    """
    if sync is None:
        return
    from grelmicro.task import _gated  # noqa: PLC0415

    _gated.refuse_leader_sync(sync)


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
    if micro is None or micro.environment in QUIET_ENVIRONMENTS:
        return
    from grelmicro.task import _gated  # noqa: PLC0415

    if not _gated.has_schedule_backend(micro):
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
