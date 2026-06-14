"""Tests proving GrelmicroMiddleware is pure ASGI (Starlette and Litestar)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from litestar import Litestar, get
from litestar.middleware import DefineMiddleware
from litestar.testing import AsyncTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.status import HTTP_200_OK, HTTP_500_INTERNAL_SERVER_ERROR

from grelmicro import Grelmicro, NoActiveAppError
from grelmicro.errors import OutOfContextError
from grelmicro.fastapi import GrelmicroMiddleware
from grelmicro.resilience import RateLimiter, RateLimiters
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, MutableMapping
    from typing import Any

    from starlette.requests import Request
    from starlette.websockets import WebSocket

pytestmark = [pytest.mark.timeout(5)]


# ---------------------------------------------------------------------------
# Starlette
# ---------------------------------------------------------------------------


def _build_starlette_app(
    *, with_middleware: bool
) -> tuple[Starlette, Grelmicro]:
    """Build a Starlette app whose handler resolves a RateLimiter ambiently."""
    micro = Grelmicro(uses=[RateLimiters(MemoryRateLimiterAdapter())])

    async def limited(request: Request) -> JSONResponse:  # noqa: ARG001
        limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
        result = await limiter.acquire(key="client")
        return JSONResponse({"allowed": result.allowed})

    async def ws_limited(websocket: WebSocket) -> None:
        await websocket.accept()
        limiter = RateLimiter.sliding_window("ws", limit=5, window=1.0)
        result = await limiter.acquire(key="client")
        await websocket.send_json({"allowed": result.allowed})
        await websocket.close()

    app = Starlette(
        routes=[
            Route("/limited", limited),
            WebSocketRoute("/ws", ws_limited),
        ],
    )
    if with_middleware:
        app.add_middleware(GrelmicroMiddleware, micro=micro)
    return app, micro


async def test_starlette_middleware_binds_app_inside_request_handler() -> None:
    """A Starlette handler resolves the ambient backend when the middleware is present."""
    app, micro = _build_starlette_app(with_middleware=True)
    async with micro:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/limited")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}


async def test_starlette_without_middleware_handler_misses_ambient_backend() -> (
    None
):
    """Without the middleware, the Starlette handler hits the ambient-miss guard."""
    app, _micro = _build_starlette_app(with_middleware=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        with pytest.raises(OutOfContextError):
            await client.get("/limited")


async def test_starlette_middleware_binds_on_websocket_scope() -> None:
    """The middleware binds the app for websocket scopes, not only http.

    Driven as a pure-ASGI websocket call on the pytest event loop, so the
    app and `async with micro:` share one loop (no cross-loop TestClient).
    """
    app, micro = _build_starlette_app(with_middleware=True)
    scope: MutableMapping[str, Any] = {
        "type": "websocket",
        "path": "/ws",
        "headers": [],
        "query_string": b"",
    }
    incoming: list[MutableMapping[str, Any]] = [{"type": "websocket.connect"}]
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> MutableMapping[str, Any]:
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async with micro:
        await app(scope, receive, send)

    payload = next(m for m in sent if m["type"] == "websocket.send")
    assert json.loads(str(payload["text"])) == {"allowed": True}


# ---------------------------------------------------------------------------
# Litestar
# ---------------------------------------------------------------------------
#
# Litestar does not expose ``app.add_middleware()``.  Register the middleware
# via ``DefineMiddleware``, which calls the constructor with ``app`` as a
# keyword argument.  GrelmicroMiddleware.__init__ accepts ``app`` positionally,
# so DefineMiddleware works without any adapter shim.
# ---------------------------------------------------------------------------


def _build_litestar_app(*, with_middleware: bool) -> tuple[Litestar, Grelmicro]:
    """Build a Litestar app whose handler resolves a RateLimiter ambiently."""
    micro = Grelmicro(uses=[RateLimiters(MemoryRateLimiterAdapter())])

    @get("/limited")
    async def handler() -> dict[str, bool]:
        limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
        result = await limiter.acquire(key="client")
        return {"allowed": result.allowed}

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:  # noqa: ARG001
        async with micro:
            yield

    middleware = (
        [DefineMiddleware(GrelmicroMiddleware, micro=micro)]  # ty: ignore[invalid-argument-type]
        if with_middleware
        else []
    )
    app = Litestar(
        route_handlers=[handler],
        middleware=middleware,
        lifespan=[lifespan],
    )
    return app, micro


async def test_litestar_middleware_binds_app_inside_request_handler() -> None:
    """A Litestar handler resolves the ambient backend when the middleware is present."""
    app, _micro = _build_litestar_app(with_middleware=True)
    async with AsyncTestClient(app=app) as client:
        response = await client.get("/limited")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}


async def test_litestar_without_middleware_handler_misses_ambient_backend() -> (
    None
):
    """Without the middleware, the Litestar handler returns 500 (OutOfContextError)."""
    app, _micro = _build_litestar_app(with_middleware=False)
    async with AsyncTestClient(app=app) as client:
        response = await client.get("/limited")
    assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR


# ---------------------------------------------------------------------------
# Middleware direct-call probes (framework-agnostic, no HTTP stack needed)
# ---------------------------------------------------------------------------


async def test_middleware_binds_on_websocket_scope() -> None:
    """The middleware sets the active app for websocket scopes."""
    micro = Grelmicro()
    seen: list[object] = []

    async def downstream(scope: object, receive: object, send: object) -> None:  # noqa: ARG001
        try:
            seen.append(Grelmicro.current())
        except NoActiveAppError:
            seen.append(None)

    middleware = GrelmicroMiddleware(downstream, micro=micro)

    async def noop_receive() -> dict[str, object]:
        return {"type": "noop"}

    async def noop_send(message: object) -> None:  # noqa: ARG001
        return None

    await middleware({"type": "websocket"}, noop_receive, noop_send)
    assert seen == [micro]
