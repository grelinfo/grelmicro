"""FastAPI route response cache: `CachedResponse`.

`grelmicro.integrations.fastapi` loads this on first access to one of its
names.
"""

from datetime import timedelta
from typing import Annotated, Any

try:
    from fastapi import Depends as _Depends

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - the reimport test walks this
    HAS_FASTAPI = False

from typing_extensions import Doc

from grelmicro.http._response_cache import declare_cached

__all__ = ["CachedResponse"]


def CachedResponse(  # noqa: N802
    *,
    ttl: Annotated[
        int | timedelta | None,
        Doc(
            "How long this route's response is served from the cache, in "
            "whole seconds or as a `timedelta`. A float is refused. "
            "Defaults to the `ttl` the registered `CachedResponses` "
            "carries."
        ),
    ] = None,
    shared: Annotated[
        bool,
        Doc(
            "On a route that requires a caller, serve one stored response "
            "to every caller the route admits, a credential included. Pass "
            "it only when the response is the same for each of them. "
            "Without it, a request carrying a credential is answered by "
            "the handler."
        ),
    ] = False,
) -> Any:  # noqa: ANN401
    """Declare that this route's response is cached.

    Declared on the route rather than called in the handler, because a hit
    is answered before the handler runs. `micro.install(app)` reads it off
    the dependency tree, so a repeated read is answered without the handler
    running:

    ```python
    from grelmicro.integrations.fastapi import CachedResponse


    @app.get("/products", dependencies=[CachedResponse(ttl=60)])
    async def list_products() -> list[Product]: ...
    ```

    It needs a registered `CachedResponses()`, which holds the store and
    the rules every route shares. Without one, nothing reads the
    declaration and the route answers from its handler every time.

    `@cached` caches what a function returns, which is not what the route
    answers: the framework still has to serialize it, and a header the
    handler set is not part of it. This caches the response.

    Only `GET` routes take it. `micro.install(app)` refuses it on a route
    that answers anything else, naming the path.

    Starlette and Litestar resolve no dependencies to hang this on, so
    they name their paths in `CachedResponses(include=...)` instead,
    with how long each is kept for.

    Read more in the [Response Cache](../http/cache.md) docs.
    """
    if not HAS_FASTAPI:  # pragma: no cover - the reimport test walks this
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")
    return _Depends(declare_cached(ttl, shared=shared))
