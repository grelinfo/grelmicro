"""Module-level `shield` decorator.

Supports:

- `@shield` (no parens): wraps with the `api` profile and default
  `timeout_errors=(TimeoutError,)`. The Shield name is the wrapped
  function's `__qualname__`.
- `@shield.internal(...)` / `@shield.api(...)` / `@shield.slow(...)`:
  builds a Shield with the matching profile and decorates.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro.resilience.shield._shield import Shield

if TYPE_CHECKING:
    from pydantic import PositiveFloat

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
        timeout_errors: tuple[type[BaseException], ...] | None = None,
        max_rate: PositiveFloat | None = None,
        cache: Any = None,  # noqa: ANN401
        cache_key: Callable[..., str] | None = None,
        fallback: Callable[[BaseException], Any]
        | Callable[[BaseException], Awaitable[Any]]
        | None = None,
    ) -> Callable[[_AsyncFn], _AsyncFn]:
        """Return a decorator that wraps a function with the chosen profile."""

        def wrap(fn: _AsyncFn) -> _AsyncFn:
            shield_name = name or getattr(fn, "__qualname__", None) or repr(fn)
            factory_method = getattr(Shield, profile)
            instance: Shield = factory_method(
                shield_name,
                timeout_errors=timeout_errors,
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
    """Callable object exposed as the module-level `shield`.

    Supports `@shield` (no parens) for the `api`-profile default and
    `@shield.internal(...)` / `@shield.api(...)` / `@shield.slow(...)`
    for the explicit forms.
    """

    def __call__(self, fn: _AsyncFn) -> _AsyncFn:
        """Wrap `fn` with the `api` profile and default `timeout_errors`."""
        name = getattr(fn, "__qualname__", None) or repr(fn)
        instance = Shield.api(name)
        wrapped = instance(fn)
        return functools.wraps(fn)(wrapped)

    internal = staticmethod(_build_profile_decorator("internal"))
    api = staticmethod(_build_profile_decorator("api"))
    slow = staticmethod(_build_profile_decorator("slow"))


shield = _ShieldDecorator()
