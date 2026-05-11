"""grelmicro is a lightweight framework/toolkit which is ideal for building async microservices in Python."""  # noqa: E501

from collections.abc import AsyncIterator, Iterable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)

from grelmicro._app import (
    ComponentAlreadyRegisteredError,
    ComponentNotRegisteredError,
    Grelmicro,
    NoActiveAppError,
)
from grelmicro._component import Component
from grelmicro._deprecation import warn_legacy


def lifespan(
    *ad_hoc: AbstractAsyncContextManager[object],
    exclude: Iterable[str] = (),
) -> AbstractAsyncContextManager[None]:
    """Open every registered backend, close them all on exit.

    Deprecated since 0.23.0, removed in 1.0.0. Build a `Grelmicro` app and
    open it with `async with micro:` instead.

    Walks every grelmicro backend registry that has been imported
    in the current process and enters each registered backend that
    is not listed in ``exclude``. Positional arguments are entered
    after the registered backends. On exit (or any failure during
    startup) every entered context manager is closed in reverse
    order.

    Modules whose registry has not been imported are skipped:
    importing ``grelmicro`` alone walks nothing. Importing
    ``grelmicro.sync`` makes the sync registry visible.

    ``exclude`` matches by dotted prefix. ``{"resilience"}`` skips
    every ``resilience.*`` registry (rate limiter and circuit
    breaker). ``{"resilience.ratelimiter"}`` skips only that
    registry. ``{"resilience.ratelimiter.analytics"}`` skips just
    the named entry inside that registry.
    """
    warn_legacy(
        "grelmicro.lifespan",
        "`async with Grelmicro(uses=[...]):`",
    )
    return _lifespan(*ad_hoc, exclude=exclude)


@asynccontextmanager
async def _lifespan(
    *ad_hoc: AbstractAsyncContextManager[object],
    exclude: Iterable[str] = (),
) -> AsyncIterator[None]:
    from grelmicro._backends import _ALL_REGISTRIES  # noqa: PLC0415

    excluded = frozenset(exclude)

    def is_excluded(path: str) -> bool:
        return any(path == ex or path.startswith(f"{ex}.") for ex in excluded)

    async with AsyncExitStack() as stack:
        for module_name, registry in _ALL_REGISTRIES.items():
            if is_excluded(module_name):
                continue
            for entry_name, backend in registry.items():
                if is_excluded(f"{module_name}.{entry_name}"):
                    continue
                await stack.enter_async_context(backend)
        for ctx in ad_hoc:
            await stack.enter_async_context(ctx)
        yield


__all__ = [
    "Component",
    "ComponentAlreadyRegisteredError",
    "ComponentNotRegisteredError",
    "Grelmicro",
    "NoActiveAppError",
    "lifespan",
]
