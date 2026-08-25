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
function underneath, which every instance of the class shares. Each mark
records the instance it was written for, and they are kept side by side,
so registering two objects' methods guards both and leaves a third
object's alone.
"""

from __future__ import annotations

import weakref
from enum import Enum
from typing import NamedTuple, cast

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


UNNAMEABLE_OWNER = object()
"""Stands in for an owner no weak reference can name."""


def _owner(function: object) -> weakref.ref[object] | object | None:
    """Return a reference to what a bound method is bound to.

    None for a plain function, which owns nothing. `UNNAMEABLE_OWNER`
    for an instance no weak reference can name, which cannot be told
    from its siblings and so is left unmarked rather than marked in a
    way that would refuse all of them.
    """
    try:
        instance = getattr(function, "__self__", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return UNNAMEABLE_OWNER
    if instance is None:
        return None
    try:
        return weakref.ref(instance)
    except TypeError:
        return UNNAMEABLE_OWNER


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
    owner = _owner(function)
    if owner is UNNAMEABLE_OWNER:
        return
    owner = cast("weakref.ref[object] | None", owner)
    fresh = Registration(kind, holder, owner)
    try:
        instance = owner() if owner is not None else None
        kept = tuple(
            existing
            for existing in _marks(target)
            if not _gone(existing) and not _superseded(existing, kind, instance)
        )
        setattr(target, REGISTRATION, (*kept, fresh))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001, S110
        pass


def _gone(registration: Registration) -> bool:
    """Return whether the instance a mark names no longer exists."""
    owner = registration.owner
    return owner is not None and owner() is None


def _superseded(
    registration: Registration, kind: Registered, instance: object
) -> bool:
    """Return whether a fresh mark says what `registration` already said.

    One function registered twice the same way needs one mark, or a
    module-level function re-registered on every app build accumulates
    them for the life of the process. Registered two different ways, it
    keeps both, so the refusal can name a registrar that really holds it.
    """
    if registration.kind is not kind:
        return False
    owner = registration.owner
    if owner is None:
        return instance is None
    return instance is not None and owner() is instance


def _marks(target: object) -> tuple[Registration, ...]:
    """Return the marks on `target`, and never raise reading them.

    Answers the unmarked function without building anything, because
    every decorated call site reads this and almost none carry a mark.
    """
    try:
        marks = getattr(target, REGISTRATION, ())
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return ()
    if not marks or not isinstance(marks, tuple):
        return ()
    return tuple(m for m in marks if isinstance(m, Registration))


def registration_of(function: object) -> Registration | None:
    """Return the registration holding `function`, or None.

    Answers None for a mark whose function is gone, for one whose
    instance is gone, and for a method of an instance other than the
    one registered, none of which a registration still holds.

    Two registrars holding one function are read most recent first,
    which is the one written outermost, because decorators apply
    upwards and that is the one the reader is looking at.

    A mark copied onto a wrapper by `functools.wraps` still answers,
    because a decorator above that wrapper misses the registration too.
    Ask `Registration.holds` to tell a copy from the function itself.

    That carries only for a plain function. Wrapping drops `__self__`,
    so a copy of a bound method cannot be told from a wrapper around
    another instance's method, and answering for it would refuse every
    sibling. A registrar that took a bound method is answered for that
    instance alone.
    """
    target = _underlying(function)
    marks = _marks(target)
    if not marks:
        return None
    try:
        bound_to = getattr(function, "__self__", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return None
    inherited: Registration | None = None
    for registration in reversed(marks):
        holder = registration.holder
        held = holder() if holder is not None else None
        if holder is not None and held is None:
            continue
        owner = registration.owner
        if owner is None:
            if held is None or held is target:
                return registration
            inherited = inherited or registration
            continue
        bound = owner()
        if bound is None:
            continue
        if bound is bound_to:
            return registration
    return inherited
