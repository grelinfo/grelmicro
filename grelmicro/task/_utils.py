"""Task Utilities.

`validate_and_generate_reference` is adapted from an upstream project.
See THIRD_PARTY_NOTICES.md for the source, license, and changes.
"""

import string
from collections.abc import Callable
from contextlib import suppress
from datetime import tzinfo
from functools import partial
from hashlib import sha256
from inspect import ismethod
from typing import Any

from grelmicro._timezone import (
    normalize_timezone_name,
)
from grelmicro._timezone import (
    resolve_timezone as _resolve_timezone,
)
from grelmicro.coordination.lock import (
    LOCK_NAME_MAX_LENGTH,
    validate_lock_name,
)
from grelmicro.errors import SettingsValidationError
from grelmicro.task.errors import FunctionTypeError, TimezoneError


def resolve_timezone(timezone: str) -> tzinfo:
    """Resolve an IANA timezone name into a ``tzinfo``.

    Raises:
        TimezoneError: If no timezone of that name can be loaded.
    """
    try:
        return _resolve_timezone(timezone)
    except ValueError as error:
        raise TimezoneError(str(error)) from None


def normalize_timezone(timezone: str) -> str:
    """Return an IANA timezone name in the casing the database uses.

    Raises:
        TimezoneError: If no timezone of that name can be loaded.
    """
    try:
        return normalize_timezone_name(timezone)
    except ValueError as error:
        raise TimezoneError(str(error)) from None


def validate_and_generate_reference(function: Callable[..., Any]) -> str:
    """Build a stable ``module:qualname`` reference for a task function.

    The reference must survive process restarts and round-trip through
    serialization, so only top-level ``def`` and ``async def`` callables
    are accepted. Anything whose identity depends on a closure, a bound
    instance, or runtime construction is rejected.

    The returned reference surfaces in logs, distributed coordination
    keys, and metric labels. For tasks that handle sensitive workflows,
    pass an explicit ``name=`` to the task decorator or registration
    call instead of relying on the auto-generated module path.

    Raises:
        FunctionTypeError: If ``function`` cannot be referenced by name.

    """
    if isinstance(function, partial):
        ref = "partial()"
        raise FunctionTypeError(ref)

    if ismethod(function):
        ref = "method"
        raise FunctionTypeError(ref)

    module = getattr(function, "__module__", None)
    qualname = getattr(function, "__qualname__", None)
    if not module or not qualname:
        ref = "callable without __module__ or __qualname__ attribute"
        raise FunctionTypeError(ref)

    if "<lambda>" in qualname:
        ref = "lambda"
        raise FunctionTypeError(ref)

    if "<locals>" in qualname:
        ref = "nested function"
        raise FunctionTypeError(ref)

    return f"{module}:{qualname}"


_LOCK_KEY_PREFIX = "task-"
_LOCK_KEY_KEPT = frozenset(string.ascii_letters + string.digits + "_.:")
_LOCK_KEY_DIGEST_LEN = 16


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
