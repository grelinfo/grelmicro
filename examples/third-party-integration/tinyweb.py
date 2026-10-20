"""tinyweb: a minimal ASGI framework, the one `grelmicro_tinyweb` integrates.

It routes by exact path and method, runs a lifespan and a middleware stack,
and answers `404` to any other request.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

    Lifespan = Callable[["TinyWeb"], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def _no_lifespan(app: TinyWeb) -> AsyncIterator[None]:
    yield


@dataclass
class Route:
    """One route: an exact path, the endpoint answering it, and what it requires."""

    path: str
    endpoint: Callable[[Scope], Awaitable[str]]
    methods: frozenset[str] = frozenset({"GET"})
    anonymous: bool = False
    scopes: frozenset[str] = frozenset()
    cache: bool | timedelta = False
    own_checks: bool = False
    app: ASGIApp = field(init=False)

    def __post_init__(self) -> None:
        """Dispatch to the endpoint, answering its text with `200`."""
        self.app = self._serve

    async def _serve(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: ARG002
        await _answer(send, 200, await self.endpoint(scope))


class TinyWeb:
    """An ASGI app with a route table, a lifespan and a middleware stack."""

    def __init__(
        self, *routes: Route, lifespan: Lifespan = _no_lifespan
    ) -> None:
        """Serve `routes`, opening `lifespan` around the app's life."""
        self.routes = list(routes)
        self.lifespan = lifespan
        self.middleware: list[tuple[type[Any], dict[str, Any]]] = []
        """Each middleware and its options, the first outermost."""
        self._stack: ASGIApp | None = None

    def add_route(self, route: Route) -> None:
        """Serve one more route."""
        self.routes.append(route)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Run the lifespan, or route the request through the middleware.

        The request's scope names the app serving it under `app`.
        """
        if scope["type"] == "lifespan":
            await self._run_lifespan(receive, send)
            return
        scope["app"] = self
        if self._stack is None:
            self._stack = self._dispatch
            for middleware, options in reversed(self.middleware):
                self._stack = middleware(self._stack, **options)
        await self._stack(scope, receive, send)

    async def _dispatch(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        for route in self.routes:
            if route.path == scope["path"] and scope["method"] in route.methods:
                await route.app(scope, receive, send)
                return
        await _answer(send, 404, "Not Found")

    async def _run_lifespan(self, receive: Receive, send: Send) -> None:
        await receive()
        phase = "startup"
        try:
            async with self.lifespan(self):
                await send({"type": "lifespan.startup.complete"})
                phase = "shutdown"
                await receive()
        except Exception as exc:
            await send(
                {"type": f"lifespan.{phase}.failed", "message": str(exc)}
            )
            raise
        await send({"type": "lifespan.shutdown.complete"})


async def _answer(send: Send, status: int, text: str) -> None:
    body = text.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
