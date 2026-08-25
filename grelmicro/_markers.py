"""Marks one part of grelmicro leaves on a function for another to read.

A registering decorator records the function it is handed and gives the
same one back, so a decorator applied above it never reaches what was
registered. The mark lets the decorator above refuse that order instead
of wrapping calls the registration will never make.

Three registrars write a mark: a task router, a health check registry,
and an outbox handler registry. Every decorator that wraps a function
reads it through `grelmicro._wrapping`.

The reader lives here rather than in the modules that write it, so
reading a mark costs no import of the packages that set it.

The mark carries a weak reference to the function it was written for.
`functools.wraps` copies `__dict__`, so a wrapper inherits the mark of
the function it wraps. That inheritance is worth keeping, because a
decorator above such a wrapper is just as absent from the registration
as one applied directly. The reference is what lets the refusal say
which of the two happened instead of claiming the wrapper itself was
registered.

A mark whose function is gone holds nothing, since a registration keeps
the function it recorded alive, so it stops counting.

A bound method carries no attribute of its own, so the mark goes on the
function underneath, which every instance of the class shares. The mark
records the instance it was written for, so registering one object's
method leaves every other object's alone.
"""

from __future__ import annotations

import weakref
from enum import Enum
from typing import NamedTuple

__all__ = [
    "Registered",
    "Registration",
    "mark_registered",
    "registration_of",
]

REGISTRATION = "__grelmicro_registration__"
"""Attribute a registrar leaves on a function it has recorded."""


class Registered(Enum):
    """What a registrar holds a function as, as it reads in a message."""

    TASK = "a task"
    HEALTH_CHECK = "a health check"
    OUTBOX_HANDLER = "an outbox handler"


class Registration(NamedTuple):
    """A mark: what registered the function, and which function it was."""

    kind: Registered
    holder: weakref.ref[object] | None
    owner: weakref.ref[object] | None

    def holds(self, function: object) -> bool:
        """Return whether this mark names `function` itself.

        False for a mark that arrived by `functools.wraps` copying the
        `__dict__` of the function it wrapped, which is registered while
        the copy is not.
        """
        holder = self.holder
        if holder is None:
            return True
        return holder() is _underlying(function)


def _owner(function: object) -> weakref.ref[object] | None:
    """Return a reference to what a bound method is bound to, or None."""
    try:
        instance = getattr(function, "__self__", None)
        if instance is None:
            return None
        return weakref.ref(instance)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return None


def _underlying(function: object) -> object:
    """Return what a bound method wraps, and never raise finding out."""
    try:
        return getattr(function, "__func__", function)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return function


def mark_registered(function: object, kind: Registered) -> None:
    """Mark `function` as recorded by a registrar.

    A bound method carries no attribute of its own, so the function
    underneath is marked instead, and the instance the method was bound
    to is recorded beside it. Another instance of the same class shares
    that function and is not what the registration holds.

    Best effort: a callable that takes no attribute goes unmarked, and
    the decorators above it keep the behaviour they had before the mark
    existed.
    """
    target = _underlying(function)
    try:
        holder: weakref.ref[object] | None = weakref.ref(target)
    except TypeError:
        holder = None
    try:
        setattr(
            target, REGISTRATION, Registration(kind, holder, _owner(function))
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001, S110
        pass


def registration_of(function: object) -> Registration | None:
    """Return the registration holding `function`, or None.

    Answers None for a mark whose function is gone, which no
    registration can still hold, and for a method of an instance other
    than the one registered, which nothing holds either. A mark copied
    onto a wrapper by `functools.wraps` still answers, because a
    decorator above that wrapper misses the registration too. Ask
    `Registration.holds` to tell the two apart.
    """
    target = _underlying(function)
    try:
        registration = getattr(target, REGISTRATION, None)
        bound_to = getattr(function, "__self__", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return None
    if not isinstance(registration, Registration):
        return None
    holder = registration.holder
    if holder is not None and holder() is None:
        return None
    owner = registration.owner
    if owner is not None and owner() is not bound_to:
        return None
    return registration
