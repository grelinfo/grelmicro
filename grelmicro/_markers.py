"""Marks one part of grelmicro leaves on a function for another to read.

A task decorator registers the function it is handed and gives the same
one back, so a decorator applied above it never reaches the schedule.
The mark lets the decorator above refuse that order instead of running
where nothing will call it.

The reader lives here rather than in the module that writes it, so
reading a mark costs no import of the package that sets it.
"""

from __future__ import annotations

from contextlib import suppress

__all__ = ["is_scheduled", "mark_scheduled"]

SCHEDULED = "__grelmicro_scheduled__"
"""Attribute a task router leaves on a function it has registered."""


def mark_scheduled(function: object) -> None:
    """Mark `function` as registered by a task router.

    A bound method carries no attribute of its own, so the function
    underneath is marked instead. Every bound form of it then reads as
    registered, which is what the schedule holds either way.
    """
    with suppress(AttributeError):
        setattr(getattr(function, "__func__", function), SCHEDULED, True)


def is_scheduled(function: object) -> bool:
    """Return whether a task router has already registered `function`."""
    target = getattr(function, "__func__", function)
    return bool(getattr(target, SCHEDULED, False))
