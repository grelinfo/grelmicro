"""Checks on the key function and key template a component takes."""

import inspect
from collections.abc import Callable
from string import Formatter
from typing import Any

from grelmicro._wrapping import named

_CONVERSIONS = frozenset({"r", "s", "a"})
"""Conversions a template field accepts after `!`."""

type KeyFunction = Callable[
    [Callable[..., Any], tuple[Any, ...], dict[str, Any]], str
]
"""A function deriving a key from a decorated call.

It receives the decorated function, the call's positional arguments, and its
keyword arguments, and returns the key string.
"""


def check_key_function(key: object) -> None:
    """Refuse a `key=` that is neither `None` nor a function.

    Raises:
        TypeError: If `key` is not callable.
    """
    if key is not None and not callable(key):
        msg = "key must be a function"
        raise TypeError(msg)


def check_key_choice(key: object, key_template: str | None) -> None:
    """Refuse a `key=` that is not a function, or both `key=` and `key_template=`.

    Raises:
        TypeError: If `key` is not a function, or both were passed.
    """
    if isinstance(key, str):
        msg = "key must be a function, use key_template= for a template"
        raise TypeError(msg)
    check_key_function(key)
    if key is not None and key_template is not None:
        msg = (
            "Pass either key= or key_template=, not both. key_template= "
            "renders the key from the call's arguments, a key= function "
            "derives it for the fully dynamic case."
        )
        raise TypeError(msg)


def template_fields(template: str, label: str) -> tuple[str, ...]:
    """Return the parameter names a key template reads.

    A field inside a format spec, as in `"{user_id:{width}}"`, is read
    too. `label` opens every message, as in `"Cache key template"`.

    Raises:
        ValueError: If the template cannot be read, reads a positional
            field, or converts a field with anything but `!r`, `!s` or
            `!a`.
    """
    fields: list[str] = []
    pending = [template]
    while pending:
        try:
            parsed = list(Formatter().parse(pending.pop()))
        except ValueError as error:
            msg = f"{label} {template!r} cannot be read: {error}."
            raise ValueError(msg) from None
        for _, field, spec, conversion in parsed:
            if field is None:
                continue
            name = field.split(".")[0].split("[")[0]
            if name == "" or name.isdigit():
                msg = (
                    f"{label} {template!r} reads a positional field. Name "
                    'the parameter instead, as in "user:{user_id}".'
                )
                raise ValueError(msg)
            if conversion is not None and conversion not in _CONVERSIONS:
                msg = (
                    f"{label} {template!r} converts {field} with "
                    f"!{conversion}. Use !r, !s or !a."
                )
                raise ValueError(msg)
            fields.append(name)
            if spec:
                pending.append(spec)
    return tuple(fields)


def check_template_parameters(
    template: str,
    fields: tuple[str, ...],
    function: Callable[..., Any],
    signature: inspect.Signature,
    label: str,
) -> None:
    """Refuse a key template naming a parameter `function` does not take.

    Raises:
        ValueError: If a name in `fields` is not a parameter of
            `signature`.
    """
    unknown = sorted(set(fields) - set(signature.parameters))
    if unknown:
        msg = (
            f"{label} {template!r} names {', '.join(unknown)}, which "
            f"{named(function)} does not take."
        )
        raise ValueError(msg)
