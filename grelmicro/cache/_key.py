"""Cache Key Generation."""

import hashlib
from collections.abc import Callable
from typing import Annotated, Any

from typing_extensions import Doc


def make_cache_key(
    func: Annotated[
        Callable[..., Any],
        Doc(
            """
            The function to generate a cache key for.
            """,
        ),
    ],
    args: Annotated[
        tuple[Any, ...],
        Doc(
            """
            Positional arguments passed to the function.
            """,
        ),
    ],
    kwargs: Annotated[
        dict[str, Any],
        Doc(
            """
            Keyword arguments passed to the function.
            """,
        ),
    ],
    *,
    typed: Annotated[
        bool,
        Doc(
            """
            If True, include argument types in the key.
            """,
        ),
    ] = False,
) -> str:
    """Generate cache key from function identity and arguments.

    The key format is:
        ``{module}.{qualname}:{sha256_hex_digest}``

    where the digest is computed from ``repr((args, sorted_kwargs))``.
    When *typed* is ``True``, argument types are included so that
    e.g. ``3`` and ``3.0`` produce different keys.

    Note:
        Keys rely on ``repr()`` which is deterministic within a single
        process but may vary across Python versions or for objects
        whose ``__repr__`` includes memory addresses.

    Returns:
        A deterministic cache key string.
    """
    module = getattr(func, "__module__", "")
    qualname = getattr(func, "__qualname__", repr(func))
    prefix = f"{module}.{qualname}"
    raw = repr((args, sorted(kwargs.items())))
    if typed:
        arg_types = tuple(type(a) for a in args)
        kwarg_types = tuple(type(v) for _, v in sorted(kwargs.items()))
        raw += repr((arg_types, kwarg_types))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{prefix}:{digest}"
