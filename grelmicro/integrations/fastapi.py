"""ASGI middleware that binds the active Grelmicro app inside request handlers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        MutableMapping,
    )

    from starlette.applications import Starlette

    from grelmicro import Grelmicro

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["GrelmicroMiddleware", "install"]


class GrelmicroMiddleware:
    """Bind the active `Grelmicro` app for the duration of each request.

    A request handler runs in its own task, outside the `async with micro:`
    block, so `Grelmicro.current()` and the ambient `backend=` resolution it
    powers do not see the app there. This middleware sets the active app for
    the request task, so `Lock("cart")`, `RateLimiter.sliding_window(...)`,
    and `@cached` resolve ambiently inside the handler exactly as they do in
    a task.

    ```python
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.integrations.fastapi import GrelmicroMiddleware

    micro = Grelmicro(uses=[...])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with micro:
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(GrelmicroMiddleware, micro=micro)
    ```

    Open the app in the framework lifespan so its components are registered
    before any request arrives. The middleware is pure ASGI and works with
    any ASGI framework (Starlette, Litestar, ...). It binds on `http` and
    `websocket` scopes and passes the `lifespan` scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        micro: Annotated[
            Grelmicro,
            Doc(
                "The `Grelmicro` app to bind for each request. Open it in "
                "the framework lifespan so its components are ready."
            ),
        ],
    ) -> None:
        """Initialize the middleware with the app to bind."""
        self.app = app
        self.micro = micro

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Bind the app on request scopes, pass other scopes through."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = self.micro._bind_current()  # noqa: SLF001
        try:
            await self.app(scope, receive, send)
        finally:
            self.micro._reset_current(token)  # noqa: SLF001


def install(
    app: Annotated[
        Starlette,
        Doc("The Starlette or FastAPI application to wire."),
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
            "Add `GrelmicroMiddleware` so patterns resolve ambiently inside "
            "request handlers. Default `True`. Pass `False` to skip it."
        ),
    ] = True,
) -> None:
    """Wire `micro` into a Starlette or FastAPI app.

    Chains `async with micro:` around the app's existing lifespan, so any
    lifespan already passed to the framework keeps running and the components
    are open before the first request. When `ambient` is `True`, adds
    `GrelmicroMiddleware` so patterns resolve through `Grelmicro.current()`
    inside request handlers.

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[Any]:
        async with previous(app) as state, micro:
            yield state

    app.router.lifespan_context = lifespan
    if ambient:
        app.add_middleware(GrelmicroMiddleware, micro=micro)
