"""grelmicro is a lightweight framework/toolkit which is ideal for building async microservices in Python."""  # noqa: E501

from collections.abc import AsyncIterator, Iterable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
)


@asynccontextmanager
async def lifespan(
    *ad_hoc: AbstractAsyncContextManager[object],
    exclude: Iterable[str] = (),
) -> AsyncIterator[None]:
    """Open every registered backend, close them all on exit.

    Walks every grelmicro backend registry that has been imported
    in the current process and enters each registered backend that
    is not listed in ``exclude``. Positional arguments are entered
    after the registered backends. On exit (or any failure during
    startup) every entered context manager is closed in reverse
    order.

    Modules whose registry has not been imported are skipped:
    importing ``grelmicro`` alone walks nothing. Importing
    ``grelmicro.sync`` makes the sync registry visible. Use
    ``exclude={"<module>"}`` to skip a module or
    ``exclude={"<module>.<name>"}`` to skip one named entry.
    """
    from grelmicro._backends import _ALL_REGISTRIES  # noqa: PLC0415

    excluded = frozenset(exclude)
    async with AsyncExitStack() as stack:
        for module_name, registry in _ALL_REGISTRIES.items():
            if module_name in excluded:
                continue
            for entry_name, backend in registry.items():
                if f"{module_name}.{entry_name}" in excluded:
                    continue
                await stack.enter_async_context(backend)
        for ctx in ad_hoc:
            await stack.enter_async_context(ctx)
        yield


__all__ = ["lifespan"]
