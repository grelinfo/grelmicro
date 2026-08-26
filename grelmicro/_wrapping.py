"""What every grelmicro decorator checks before it wraps a function.

A registering decorator records the function it is handed and returns
the same one, so a decorator applied below it wraps a name nothing will
call. Refusing that order is the same check in every decorator, and a
check pasted once per decorator is one the next decorator forgets.

The order the guard names is the one the composition docs give.
"""

from __future__ import annotations

from grelmicro._markers import registration_of

__all__ = ["named", "refuse_registered"]


def named(function: object) -> str:
    """Return the name of a decorated function, for a message."""
    try:
        return getattr(function, "__qualname__", None) or repr(function)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return "<unnameable>"


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
