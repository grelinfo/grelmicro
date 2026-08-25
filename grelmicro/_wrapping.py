"""What every grelmicro decorator checks before it wraps a function.

A registering decorator records the function it is handed and returns
the same one, so a decorator applied below it wraps a name nothing will
call. Refusing that order is the same check in every decorator, and a
check pasted once per decorator is one the next decorator forgets.

Read more in the [composition](../resilience/composition.md) docs.
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
    name = named(function)
    noun = registration.kind.value
    subject = (
        f"{name} is already registered as {noun}"
        if registration.holds(function)
        else f"{name} wraps a function already registered as {noun}"
    )
    msg = (
        f"{subject}, so the registration holds what it recorded and "
        f"{by} would only wrap direct calls. Put the decorator that "
        f"registers it on top, above {by}. To wrap direct calls alone, "
        f"apply {by} to a function that calls {name}."
    )
    raise TypeError(msg)
