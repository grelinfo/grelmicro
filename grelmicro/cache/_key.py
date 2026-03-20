"""Cache Key Generation."""

import hashlib
from collections.abc import Callable
from typing import Any


def make_cache_key(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Generate cache key from function identity and arguments.

    The key format is:
        ``{module}.{qualname}:{sha256_hex_digest}``

    where the digest is computed from ``repr((args, sorted_kwargs))``.

    Note:
        Keys rely on ``repr()`` which is deterministic within a single
        process but may vary across Python versions or for objects
        whose ``__repr__`` includes memory addresses.

    Args:
        func: The function to generate a cache key for.
        args: Positional arguments passed to the function.
        kwargs: Keyword arguments passed to the function.

    Returns:
        A deterministic cache key string.
    """
    module = getattr(func, "__module__", "")
    qualname = getattr(func, "__qualname__", repr(func))
    prefix = f"{module}.{qualname}"
    raw = repr((args, sorted(kwargs.items())))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{prefix}:{digest}"
