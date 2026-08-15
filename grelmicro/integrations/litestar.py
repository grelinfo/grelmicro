"""Litestar integration that opens a Grelmicro app and binds it per request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from grelmicro.integrations.fastapi import GrelmicroMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from litestar import Litestar

    from grelmicro import Grelmicro

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["GrelmicroMiddleware", "install", "is_bound"]


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
