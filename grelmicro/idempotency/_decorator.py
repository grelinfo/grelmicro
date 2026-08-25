"""Idempotent Decorator."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Annotated, Any, ParamSpec, TypeVar, cast

from typing_extensions import Doc

from grelmicro._wrapping import refuse_registered

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from grelmicro.idempotency._idempotency import Idempotency

# Decorator factories cannot use PEP 695 cleanly: the inner `decorator`
# would inherit `idempotent`'s type parameters instead of being
# fresh-generic per decoration site. Module-level `ParamSpec`/`TypeVar`
# is the working pattern, hence the `UP047` suppression below.
P = ParamSpec("P")
R = TypeVar("R")


def idempotent(
    idempotency: Annotated[
        Idempotency[Any],
        Doc(
            """
            The `Idempotency` instance that stores and replays responses.

            Deliberately `Idempotency[Any]`: binding it to `R` would make
            the common `Idempotency("charge")` form, which is what the docs
            show, solve `R` as `Any` and erase the decorated return type.
            The wrapper casts the stored value back to `R` instead, so the
            function's own annotation is what survives.
            """
        ),
    ],
    *,
    key: Annotated[
        Callable[..., str],
        Doc(
            """
            Derive the idempotency key from the call arguments. Receives
            the same positional and keyword arguments as the decorated
            function and returns the key string. Left untyped on purpose:
            binding it to the decorated signature would reject the
            documented `lambda **kw: kw["idempotency_key"]` form, because
            the lambda would fix the signature the function has to match.
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
) -> Callable[
    [Callable[P, Coroutine[Any, Any, R]]],
    Callable[P, Coroutine[Any, Any, R]],
]:
    """Make an async function idempotent on a per-call key.

    On a first call for a key, the function runs and its return value is
    stored. A later call with the same key within the configured `ttl`
    replays the stored value without running the function again. A
    failing call stores nothing, so a later retry executes fresh.

    The decorated function must be a coroutine function.

    Returns:
        A decorator that makes the function idempotent.
    """

    def decorator(
        func: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        refuse_registered(func, "@idempotent")

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            call_key = key(*args, **kwargs)
            call_fingerprint = (
                fingerprint(*args, **kwargs)
                if fingerprint is not None
                else None
            )
            return cast(
                "R",
                await idempotency.run(
                    call_key,
                    lambda: func(*args, **kwargs),
                    fingerprint=call_fingerprint,
                ),
            )

        return wrapper

    return decorator
