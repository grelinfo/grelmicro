"""Component protocol for the Grelmicro app object."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from types import TracebackType


@runtime_checkable
class Component(
    AbstractAsyncContextManager["Component", bool | None], Protocol
):
    """A grelmicro component attached to a `Grelmicro` app.

    Each grelmicro component ships one microservice pattern
    (distributed lock, cache, rate limiter, circuit breaker, task scheduler,
    health check, ...). The user composes components into a `Grelmicro`
    application; the app opens every component in registration order and
    closes them in reverse order on exit.

    Attributes:
        kind: Stable identifier for the component category (`"sync"`,
            `"cache"`, `"task"`, `"health"`, ...). The app exposes the
            component on `micro.<kind>` after registration.
        name: Registration name. Multiple components of the same `kind` may
            coexist under different names. The composite key for resolution
            is `(kind, name)`.

    Example:
        ```python
        class Tasks:
            kind = "task"

            def __init__(self, *, name: str = "default") -> None:
                self.name = name

            async def __aenter__(self) -> Self: ...
            async def __aexit__(self, exc_type, exc, tb) -> bool | None: ...
        ```
    """

    kind: ClassVar[str]
    name: str

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...
