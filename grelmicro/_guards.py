"""Shape tests that never raise on a value the caller supplied.

`isinstance` and `issubclass` look like inspection and are not. Both read
`__class__`, which a lazy proxy forwards to an object that raises while
unbound, and both run `__instancecheck__` or `__subclasscheck__`, which a
metaclass can define. `issubclass` refuses a non-class outright.

That matters wherever the answer decides how a value is handled rather
than what it is: inside a pydantic validator, where only `ValueError` and
`AssertionError` are converted, and inside a matcher, which runs where a
raised error replaces the one being handled. Answering False sends the
value to the argument error it was going to get anyway.

A real `KeyboardInterrupt` or `SystemExit` still propagates.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "is_class",
    "is_instance",
    "is_subclass",
    "name_of",
    "type_name",
]


def is_instance(value: Any, parent: Any) -> bool:  # noqa: ANN401
    """Return whether `value` is a `parent`, and never raise deciding it."""
    try:
        return isinstance(value, parent)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False


def is_class(candidate: Any) -> bool:  # noqa: ANN401
    """Return whether `candidate` is a class, and never raise deciding it."""
    return is_instance(candidate, type)


def is_subclass(candidate: Any, parent: type) -> bool:  # noqa: ANN401
    """Return whether `candidate` subclasses `parent`, and never raise.

    An object whose `__class__` reports `type` reaches `issubclass`, which
    refuses it. Answering False sends it to the argument error the caller
    is meant to see.
    """
    try:
        return issubclass(candidate, parent)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False


UNNAMEABLE = "<unnameable>"
"""Stands in for a value that refuses every attempt to name it."""


def type_name(value: Any) -> str:  # noqa: ANN401
    """Name a value by its type, and never raise doing it.

    Names the type, never the value: a rejected argument holds caller
    data, and an error message is rendered verbatim. A metaclass can
    define `__name__` as a property that raises, so even this reads
    caller code.
    """
    try:
        return _exact(str(type(value).__name__))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return UNNAMEABLE


def name_of(value: Any) -> str:  # noqa: ANN401
    """Name a class by itself and anything else by its type, never raising.

    `ValueError` reads as `ValueError`, and an instance reads as the class
    it came from, so a message names the thing the caller passed without
    printing what it holds.
    """
    try:
        if is_class(value):
            return _exact(str(value.__name__))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return UNNAMEABLE
    return type_name(value)


def _exact(text: str) -> str:
    """Return `text` as an exact `str`, whatever subclass it arrived as.

    `str()` may hand back a subclass, and a subclass runs caller code
    again from `__format__` or `__str__` the moment the name is
    interpolated into the message it was read for.
    """
    return str.__str__(text)
