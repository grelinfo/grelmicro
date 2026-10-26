"""FastAPI route rate limit: `RateLimited`.

`grelmicro.integrations.fastapi` loads this on first access to one of its
names.
"""

import logging
from collections.abc import Callable
from typing import Annotated, Any

try:
    # FastAPI reads the dependency's annotations at runtime, from this
    # module, so they are imported here.
    from fastapi import Depends as _Depends
    from fastapi import Request as _Request
    from fastapi import Response as _Response

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - the reimport test walks this
    HAS_FASTAPI = False

from typing_extensions import Doc

from grelmicro.http._ratelimit import (
    bucket_of,
    check_route_limiters,
    spend,
    state_on,
)
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter import RateLimiter
from grelmicro.security.clientip import TrustedProxies

__all__ = ["RateLimited"]

_logger = logging.getLogger("grelmicro.integrations.fastapi")


def RateLimited(  # noqa: N802
    *limiters: Annotated[
        RateLimiter,
        Doc("The limiters this route spends, in the order given."),
    ],
    cost: Annotated[
        int,
        Doc("Tokens one call of this route spends of each."),
    ] = 1,
    max_wait: Annotated[
        float,
        Doc(
            "Seconds a throttled call waits before it is refused. `0.0` "
            "refuses as soon as the budget is spent."
        ),
    ] = 0.0,
    key: Annotated[
        Callable[[Any], str | None] | None,
        Doc(
            "Builds the bucket key from the ASGI scope, replacing the "
            "resolved caller, the same as the middleware takes. Return "
            "`None` to leave a call unmetered."
        ),
    ] = None,
    trusted: Annotated[
        TrustedProxies | None,
        Doc(
            "The proxies whose forwarded entries may be believed. Not "
            "needed when a middleware already resolved the caller."
        ),
    ] = None,
    legacy_headers: Annotated[
        bool,
        Doc("Also send the superseded `X-RateLimit-*` fields."),
    ] = False,
) -> Any:  # noqa: ANN401
    """Meter one route, on top of whatever the app meters.

    `RateLimitedRequests(...)` meters the whole app. Declare this on the
    route that costs more than the rest, or that has a quota of its own:

    ```python
    from grelmicro.integrations.fastapi import RateLimited


    @app.post("/search", dependencies=[RateLimited(searches, cost=5)])
    async def search(query: str) -> list[Hit]: ...
    ```

    Allowed or refused, the response states what the caller has left in
    the `RateLimit` and `RateLimit-Policy` fields, and a refusal answers
    `429` through the same path every other rejection takes.

    A route that returns a `Response` of its own needs a registered
    `RateLimitedRequests(...)` for those fields to reach the wire: a
    framework merges what a dependency states into the response it
    builds itself, and not into one a handler already built. The tokens
    are spent either way.

    The caller is the address a middleware already resolved, or the one
    `trusted` resolves here. Without either, and without `key`, the call
    is left unmetered rather than metered under the ingress.

    Read more in the [Rate Limit](../http/rate-limit.md) docs.
    """
    if not HAS_FASTAPI:  # pragma: no cover - the reimport test walks this
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")
    if not limiters:
        msg = (
            "RateLimited() takes at least one limiter. Declaring it with "
            "none would meter nothing while reporting that it does."
        )
        raise TypeError(msg)
    check_route_limiters(limiters, cost=cost)
    reported: list[bool] = []

    async def metered(request: _Request, response: _Response) -> None:
        """Spend this call's tokens, and state what is left."""
        scope = request.scope
        bucket = bucket_of(scope, key=key, trusted=trusted)
        if bucket.key is None:
            if not reported:
                reported.append(True)
                _logger.warning(
                    "RateLimited() found no caller to meter on %s, so this "
                    "route is metered not at all: %s",
                    scope.get("path", "the route"),
                    "trusted= names a proxy that forwarded no caller, so "
                    "every caller behind it would share one budget"
                    if bucket.degraded
                    else "nothing resolved a client address, and neither "
                    "trusted= nor key= names another bucket",
                )
            return
        try:
            stated = await spend(
                limiters,
                bucket.key,
                cost=cost,
                max_wait=max_wait,
                legacy_headers=legacy_headers,
            )
        except RateLimitExceededError as refusal:
            state_on(scope, getattr(refusal, "headers", {}))
            raise
        # The framework merges these into a response it builds itself,
        # and not into one the handler returned, so the middleware is
        # handed them too and states them whatever the route answered.
        for name, value in state_on(scope, stated).items():
            response.headers[name] = value

    return _Depends(metered)


RateLimited.__module__ = "grelmicro.integrations.fastapi"
