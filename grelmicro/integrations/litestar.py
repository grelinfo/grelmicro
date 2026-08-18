"""Litestar integration that opens a Grelmicro app and binds it per request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from grelmicro.http._problem import (
    HANDLED,
    PROBLEM_MEDIA_TYPE,
    body_of,
    framework_headers_of,
    problem_detail,
)
from grelmicro.integrations.fastapi import GrelmicroMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from litestar import Litestar, Request
    from litestar.response import Response

    from grelmicro import Grelmicro

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "GrelmicroMiddleware",
    "install",
    "install_problem_details",
    "is_bound",
]


def install(
    app: Annotated[
        Litestar,
        Doc("The Litestar application to wire."),
    ],
    micro: Annotated[
        Grelmicro,
        Doc(
            "The `Grelmicro` app to open in the lifespan and bind per request."
        ),
    ],
    *,
    ambient: Annotated[
        bool,
        Doc(
            "Wrap the app's ASGI handler with `GrelmicroMiddleware` so patterns "
            "resolve ambiently inside route handlers. Default `True`. Pass "
            "`False` to skip it."
        ),
    ] = True,
) -> None:
    """Wire `micro` into a Litestar app.

    Opens `async with micro:` on startup and closes it after shutdown, so the
    components are registered before the first request. Startup hooks and
    lifespan managers already passed to `Litestar(...)` keep running.

    When `ambient` is `True`, wraps the app's ASGI handler so patterns resolve
    through `Grelmicro.current()` inside route handlers. The wrap sits outside
    every middleware Litestar built, so one that resolves a backend ambiently
    always runs inside the request scope.

    Call it after the app is built, since Litestar builds its middleware stack
    at construction time:

    ```python
    from litestar import Litestar

    from grelmicro import Grelmicro

    micro = Grelmicro(uses=[...])
    app = Litestar(route_handlers=[...])
    micro.install(app)
    ```

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """

    async def _open_micro() -> None:
        await micro.__aenter__()

    async def _close_micro() -> None:
        if micro._exit_stack is not None:  # noqa: SLF001
            await micro.__aexit__(None, None, None)

    app.on_startup.append(_open_micro)
    app.on_shutdown.append(_close_micro)

    if not ambient:
        micro._on_ambient_disabled()  # noqa: SLF001
    elif not is_bound(app):
        # Litestar types its handler with its own ASGI aliases, which are
        # narrower than the mappings a pure-ASGI middleware accepts.
        handler = cast("ASGIApp", app.asgi_handler)
        app.asgi_handler = cast(
            "Any", GrelmicroMiddleware(handler, micro=micro)
        )


def install_problem_details(
    app: Annotated[
        Litestar,
        Doc("The Litestar application to wire."),
    ],
) -> None:
    """Render grelmicro rejections as problem details on a Litestar app.

    Registers one exception handler per rejection grelmicro raises to turn a
    caller away, so a rate limiter, a bulkhead, an open circuit breaker, an
    elapsed deadline, or an idempotency conflict answers the client with an
    `application/problem+json` body instead of a `500`.

    Litestar looks a handler up through the raised exception's class
    hierarchy, so registering `AdmissionError` covers every rejection under
    it, including one a later release adds.

    Call it after the app is built and before it serves, since Litestar
    resolves each route's handlers on its first request. `micro.install(app)`
    calls this, so a direct call is only for an app that never goes through
    `install`, or after `install(app, problem_details=False)`.

    ```python
    from grelmicro.integrations.litestar import install_problem_details

    install_problem_details(app)
    ```

    Read more in the [Problem Details](../http/problems.md) docs.
    """
    for klass in HANDLED:
        # A handler passed to `Litestar(exception_handlers=...)` wins, the
        # same way one registered before `install` does on Starlette.
        if klass not in app.exception_handlers:
            app.exception_handlers[klass] = _problem_response


def _problem_response(request: Request, exc: Exception) -> Response:
    """Render one rejection, taking the occurrence from the request path."""
    from litestar.response import Response as LitestarResponse  # noqa: PLC0415

    problem = problem_detail(exc, instance=request.url.path)
    if problem is None:  # pragma: no cover
        raise exc
    return LitestarResponse(
        content=body_of(problem),
        status_code=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=framework_headers_of(problem),
    )


def is_bound(
    app: Annotated[
        Litestar,
        Doc("The Litestar application to inspect."),
    ],
) -> bool:
    """Return whether the per-request binding middleware is in place.

    Called by `Grelmicro.check_ambient_binding` and `Grelmicro.describe` to
    catch an app that never had `micro.install(app)` called on it. True for
    both the wrap `install` adds and a `DefineMiddleware(GrelmicroMiddleware)`
    passed to `Litestar(middleware=[...])`.
    """
    if isinstance(getattr(app, "asgi_handler", None), GrelmicroMiddleware):
        return True
    return any(
        getattr(entry, "middleware", None) is GrelmicroMiddleware
        for entry in getattr(app, "middleware", ())
    )
