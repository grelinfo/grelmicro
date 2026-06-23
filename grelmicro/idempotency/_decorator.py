"""Idempotent Decorator."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Annotated, Any, ParamSpec, TypeVar

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Callable

    from grelmicro.idempotency._idempotency import Idempotency

# Decorator factories cannot use PEP 695 cleanly: the inner `decorator`
# would inherit `idempotent`'s type parameters instead of being
# fresh-generic per decoration site. Module-level `ParamSpec`/`TypeVar`
# is the working pattern.
P = ParamSpec("P")
R = TypeVar("R")


def idempotent(
    idempotency: Annotated[
        Idempotency[Any],
        Doc("The `Idempotency` instance that stores and replays responses."),
    ],
    *,
    key: Annotated[
        Callable[..., str],
        Doc(
            """
            Derive the idempotency key from the call arguments. Receives
            the same positional and keyword arguments as the decorated
            function and returns the key string.
            """,
        ),
    ],
    fingerprint: Annotated[
        Callable[..., str] | None,
        Doc(
            """
            Optional payload fingerprint derived from the call arguments.
            Receives the same arguments as the decorated function. A
            replay with a different fingerprint raises
            `IdempotencyConflictError`. When None, the instance default
            applies.
            """,
        ),
    ] = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Make an async function idempotent on a per-call key.

    On a first call for a key, the function runs and its return value is
    stored. A later call with the same key within the configured `ttl`
    replays the stored value without running the function again. A
    failing call stores nothing, so a later retry executes fresh.

    The decorated function must be a coroutine function.

    Returns:
        A decorator that makes the function idempotent.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            call_key = key(*args, **kwargs)
            call_fingerprint = (
                fingerprint(*args, **kwargs)
                if fingerprint is not None
                else None
            )
            return await idempotency.run(
                call_key,
                lambda: func(*args, **kwargs),
                fingerprint=call_fingerprint,
            )

        return wrapper  # ty: ignore[invalid-return-type]

    return decorator
