"""Component protocol for the Grelmicro app object."""

from __future__ import annotations

import inspect
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from types import TracebackType


def _needs_constructor_arguments(source: type) -> bool:
    """Return True if `source` cannot be constructed with no arguments.

    Reads the signature rather than calling and catching, so a `TypeError`
    raised from inside a zero-argument `__init__` is never mistaken for the
    class needing arguments. A signature that cannot be read (some
    C-implemented types) reads as constructible, leaving the call itself to
    decide.
    """
    try:
        parameters = inspect.signature(source).parameters
    except (TypeError, ValueError):
        return False
    return any(
        parameter.default is inspect.Parameter.empty
        and parameter.kind
        not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
        for parameter in parameters.values()
    )


def instantiate_if_class[T](source: T | type[T]) -> T:
    """Instantiate `source` if it is a bare class, else return it unchanged.

    Lets `Grelmicro(uses=[...])` and Component constructors accept either an
    instance or a zero-arg class, in the spirit of FastAPI's `Depends(dep)`:
    pass the reference, the framework calls it. A class that needs
    constructor arguments raises a clear error pointing at the fix.

    A `TypeError` raised from inside the constructor propagates untouched. The
    arity check happens before the call, so the two failures never blur
    together.

    Raises:
        TypeError: If `source` is a class whose constructor requires
            arguments, so it cannot be passed bare.
    """
    if not isinstance(source, type):
        return source
    if _needs_constructor_arguments(source):
        msg = (
            f"{source.__name__} needs constructor arguments, so it cannot be "
            f"passed as a bare class. Instantiate it first, for example "
            f"{source.__name__}(...)."
        )
        raise TypeError(msg)
    return source()


@runtime_checkable
class Component(
    AbstractAsyncContextManager["Component", bool | None], Protocol
):
    """A grelmicro component attached to a `Grelmicro` app.

    Each grelmicro component wires one microservice pattern into the app
    (distributed lock, cache, rate limiter, circuit breaker, health check,
    ...). The user composes components into a `Grelmicro` application. The app
    opens every component in registration order and closes them in reverse
    order on exit.

    Attributes:
        kind: Stable identifier for the component category (`"coordination"`,
            `"cache"`, `"ratelimiter"`, `"health"`, ...). The app exposes the
            component on `micro.<kind>` after registration.
        name: Read-only registration name. Multiple components of the same
            `kind` may coexist under different names. The composite key for
            resolution is `(kind, name)`.
        singleton: Optional class flag. When `True`, the app refuses to
            register a second component of the same `kind`. Set it on
            components that configure process-global state (the root logger,
            an OTel provider), and on any pair that cannot both be active:
            two components share a `kind` exactly when only one of them may
            answer. Absent means `False`.
        singleton_reason: Optional class string saying why a second
            registration is refused, rendered into the error. Defaults to
            the process-global-state explanation, which is wrong for a pair
            excluded for any other reason.

    Example:
        ```python
        class Mailer:
            kind = "mailer"

            def __init__(self, *, name: str = "default") -> None:
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def __aenter__(self) -> Self: ...
            async def __aexit__(self, exc_type, exc, tb) -> bool | None: ...
        ```
    """

    kind: ClassVar[str]

    @property
    def name(self) -> str: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> bool | None: ...


type Usable = (
    AbstractAsyncContextManager[object]
    | type[AbstractAsyncContextManager[object]]
)
"""One item `Grelmicro(uses=[...])`, `Bulkhead(uses=[...])`, or `micro.use()` accepts.

Covers `Component` instances, `Provider` instances, first-party backends, the
bare classes of any of those, and plain async context managers. Name it to
annotate a list you build before passing it in:

```python
from grelmicro import Grelmicro, Usable

components: list[Usable] = [Log(), health]
if settings.store_backend == "redis":
    components.append(RedisProvider())

micro = Grelmicro(uses=components)
```

`Component` names a narrower set. It excludes `Provider` instances and plain
async context managers, both of which `uses=` accepts.

A `uses=` list also takes `None` entries and skips them, which this alias does
not cover, since `micro.use(None)` is an error. Annotate a prebuilt list that
carries its own conditionals as `list[Usable | None]`.
"""
