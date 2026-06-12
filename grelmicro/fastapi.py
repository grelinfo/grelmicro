"""ASGI middleware that binds the active Grelmicro app inside request handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from grelmicro import Grelmicro

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["GrelmicroMiddleware"]


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
    from grelmicro.fastapi import GrelmicroMiddleware

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
