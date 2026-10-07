"""Checks on the key function and key template a component takes."""

from collections.abc import Callable
from typing import Any

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
