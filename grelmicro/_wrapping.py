"""What every grelmicro decorator checks before it wraps a function.

A registering decorator records the function it is handed and returns
the same one, so a decorator applied below it wraps a name nothing will
call. Refusing that order is the same check in every decorator, and a
check pasted once per decorator is one the next decorator forgets.

The order the guard names is the one the composition docs give.

A generator function runs its body while it is iterated, so a decorator
that wraps the call wraps building the generator and nothing else. The
resilience patterns refuse one with the same check and the same message.
"""

from __future__ import annotations

from inspect import (
    CO_ASYNC_GENERATOR,
    CO_GENERATOR,
    isasyncgenfunction,
    isgeneratorfunction,
)
from types import FunctionType

from grelmicro._markers import registration_of

__all__ = [
    "is_async_generator",
    "is_generator",
    "named",
    "refuse_generator",
    "refuse_registered",
    "warn_generator",
]

_GENERATOR_FLAGS = CO_ASYNC_GENERATOR | CO_GENERATOR
"""Code flags of a function whose body runs while it is iterated."""


def named(function: object) -> str:
    """Return the name of a decorated function, for a message."""
    try:
        return getattr(function, "__qualname__", None) or repr(function)
    except KeyboardInterrupt, SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        return "<unnameable>"


def is_generator(function: object) -> bool:
    """Return whether calling `function` builds a generator instead of running.

    A plain function answers from its own code flags, which is the check
    `Stack.run` pays on every call. A method, a partial and a callable
    object whose `__call__` is a generator answer too.
    """
    if type(function) is FunctionType:
        return bool(function.__code__.co_flags & _GENERATOR_FLAGS)
    if isasyncgenfunction(function) or isgeneratorfunction(function):
        return True
    call = getattr(type(function), "__call__", None)  # noqa: B004
    return call is not None and (
        isasyncgenfunction(call) or isgeneratorfunction(call)
    )


def is_async_generator(function: object) -> bool:
    """Return whether calling `function` builds an async generator.

    A method, a partial and a callable object whose `__call__` is an
    async generator function answer too.
    """
    return isasyncgenfunction(function) or isasyncgenfunction(
        getattr(type(function), "__call__", None)  # noqa: B004
    )


def generator_message(function: object, by: str) -> str:
    """Return the message that refuses a generator `function`."""
    return (
        f"{by} does not wrap {named(function)}, because a generator runs "
        "its body while it is iterated and not when it is called, so "
        f"{by} would only wrap building the generator. Wrap the function "
        "that consumes it, or the call inside the body."
    )


def refuse_generator(function: object, by: str) -> None:
    """Refuse a generator function, whose body no wrapper around the call reaches.

    Args:
        function: The function about to be wrapped.
        by: What is wrapping it, as it reads in the message, for
            example `"Retry 'db'"` or `"@shield.api"`.

    Raises:
        TypeError: If `function` is a generator or async generator
            function, or a callable object whose `__call__` is one.
    """
    if is_generator(function):
        raise TypeError(generator_message(function, by))


def warn_generator(function: object, by: str) -> None:
    """Warn that a generator `function` is refused from 0.47.0.

    For a decorator that still wraps a generator function until then. The
    warning is a `Grelmicro047Warning` at the first line outside grelmicro,
    which is the line that applied the decorator.

    Args:
        function: The function about to be wrapped.
        by: What is wrapping it, as it reads in the message, for
            example `"Retry 'db'"` or `"@fallback"`.
    """
    if is_generator(function):
        from grelmicro._deprecation import generator_function  # noqa: PLC0415

        generator_function(function, by).warn(outside=True)


def refuse_registered(function: object, by: str) -> None:
    """Refuse a function a registrar already holds.

    Args:
        function: The function about to be wrapped.
        by: What is wrapping it, as it reads in the message, for
            example `"Retry 'db'"` or `"@cached"`.

    Raises:
        TypeError: If a registrar already holds `function`, so the
            wrapper would apply to direct calls alone.
    """
    registration = registration_of(function)
    if registration is None:
        return
    noun = registration.kind.value
    if registration.holds(function):
        name = named(function)
        msg = (
            f"{name} is already registered as {noun}, so the "
            f"registration holds what it recorded and {by} would only "
            f"wrap direct calls. Put the decorator that registers it on "
            f"top, above {by}. To wrap direct calls alone, apply {by} to "
            f"a function that calls {name}."
        )
        raise TypeError(msg)
    holder = registration.holder
    registered = named(holder() if holder is not None else None)
    msg = (
        f"{by} was applied to a wrapper around {registered}, which is "
        f"already registered as {noun}. `functools.wraps` copies the "
        f"name, so the wrapper reads as {registered} and the "
        f"registration still holds what it recorded, which leaves {by} "
        f"wrapping direct calls alone. Put the decorator that registers "
        f"it on top, above {by}. To wrap direct calls alone, apply {by} "
        f"to a function that calls {registered} without copying its "
        f"name, so `functools.wraps` does not carry the registration "
        f"across."
    )
    raise TypeError(msg)
