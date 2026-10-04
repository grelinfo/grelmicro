"""`add_context` in a request or message handler.

`micro.install(app)` opens a context frame for each request and each
message, so a handler adds fields without opening a span, and the next
request starts empty.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from faststream import FastStream
from faststream.redis import RedisBroker, TestRedisBroker
from litestar import Litestar, get
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from grelmicro import Grelmicro
from grelmicro.trace import add_context, get_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.requests import Request

pytestmark = [pytest.mark.timeout(5)]


def _fastapi_app() -> FastAPI:
    app = FastAPI()
    Grelmicro().install(app)

    @app.get("/add")
    async def add() -> dict[str, Any]:
        add_context(order_id="42")
        return get_context()

    @app.get("/read")
    async def read() -> dict[str, Any]:
        return get_context()

    return app


def _starlette_app() -> Starlette:
    async def add(_: Request) -> JSONResponse:
        add_context(order_id="42")
        return JSONResponse(get_context())

    async def read(_: Request) -> JSONResponse:
        return JSONResponse(get_context())

    app = Starlette(routes=[Route("/add", add), Route("/read", read)])
    Grelmicro().install(app)
    return app


def _litestar_app() -> Litestar:
    @get("/add")
    async def add() -> dict[str, Any]:
        add_context(order_id="42")
        return get_context()

    @get("/read")
    async def read() -> dict[str, Any]:
        return get_context()

    app = Litestar(route_handlers=[add, read])
    Grelmicro().install(app)
    return app


_ASGI_APPS: dict[str, Callable[[], Any]] = {
    "fastapi": _fastapi_app,
    "starlette": _starlette_app,
    "litestar": _litestar_app,
}


@pytest.mark.parametrize("framework", sorted(_ASGI_APPS))
def test_add_context_in_request_handler_adds_fields(framework: str) -> None:
    """A request handler adds fields with add_context, no span needed."""
    # Arrange
    app = _ASGI_APPS[framework]()
    client_type = LitestarTestClient if framework == "litestar" else TestClient

    # Act
    with client_type(app) as client:
        added = client.get("/add").json()
        read = client.get("/read").json()

    # Assert
    assert added == {"order_id": "42"}
    assert read == {}


@asynccontextmanager
async def _running(app: FastStream) -> AsyncIterator[None]:
    await app.start()
    try:
        yield
    finally:
        await app.stop()


async def test_add_context_in_message_handler_adds_fields() -> None:
    """A FastStream handler adds fields with add_context, no span needed."""
    # Arrange
    broker = RedisBroker()
    app = FastStream(broker)
    Grelmicro().install(app)

    @broker.subscriber("orders")
    async def handle(order_id: str) -> dict[str, Any]:
        add_context(order_id=order_id)
        return get_context()

    # Act
    async with TestRedisBroker(broker), _running(app):
        response = await broker.request("42", "orders")
        added = await response.decode()

    # Assert
    assert added == {"order_id": "42"}
    assert get_context() == {}
