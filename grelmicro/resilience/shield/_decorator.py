"""Module-level `shield` decorator.

`@shield.internal(...)` / `@shield.api(...)` / `@shield.slow(...)`
builds a Shield with the matching profile and decorates.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro._wrapping import refuse_registered
from grelmicro.metrics._naming import callable_name
from grelmicro.resilience.shield._shield import Shield

if TYPE_CHECKING:
    from pydantic import PositiveFloat

    from grelmicro.resilience._when import WhenInput

__all__ = ["shield"]


_AsyncFn = Callable[..., Awaitable[Any]]


def _build_profile_decorator(
    profile: str,
) -> Callable[..., Callable[[_AsyncFn], _AsyncFn]]:
    """Return a decorator factory for one profile.

    The returned callable accepts `name=` plus the same kwargs as
    `Shield.api(...)` and returns the actual decorator. When `name=`
    is omitted, the wrapped function's `__qualname__` is used.
    """

    def factory(
        name: Annotated[
            str | None,
            Doc(
                "Optional Shield name. Defaults to the wrapped "
                "function's `__qualname__`."
            ),
        ] = None,
        *,
        when: WhenInput | None = None,
        max_rate: PositiveFloat | None = None,
        cache: Any = None,  # noqa: ANN401
        cache_key: Callable[..., str] | None = None,
        fallback: Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None = None,
    ) -> Callable[[_AsyncFn], _AsyncFn]:
        """Return a decorator that wraps a function with the chosen profile.

        Raises:
            TypeError: If called with the function itself, as a bare
                `@shield.api` without parentheses would.
        """
        if callable(name):
            msg = (
                f"@shield.{profile} needs parentheses and when=, such as "
                f"@shield.{profile}(when=httpx.HTTPError)"
            )
            raise TypeError(msg)

        def wrap(fn: _AsyncFn) -> _AsyncFn:
            refuse_registered(fn, f"@shield.{profile}")
            shield_name = name or callable_name(fn)
            factory_method = getattr(Shield, profile)
            instance: Shield = factory_method(
                shield_name,
                when=when,
                max_rate=max_rate,
                cache=cache,
                cache_key=cache_key,
                fallback=fallback,
            )
            wrapped = instance(fn)
            return functools.wraps(fn)(wrapped)

        return wrap

    return factory


class _ShieldDecorator:
    """Namespace exposed as the module-level `shield`.

    Holds `@shield.internal(...)`, `@shield.api(...)` and
    `@shield.slow(...)`, one per profile.
    """

    internal = staticmethod(_build_profile_decorator("internal"))
    api = staticmethod(_build_profile_decorator("api"))
    slow = staticmethod(_build_profile_decorator("slow"))


shield = _ShieldDecorator()
