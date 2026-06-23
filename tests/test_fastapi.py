"""Tests for the Grelmicro ASGI context middleware."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.status import HTTP_200_OK

from grelmicro import Grelmicro
from grelmicro.errors import OutOfContextError
from grelmicro.fastapi import GrelmicroMiddleware
from grelmicro.resilience import RateLimiter, RateLimiterRegistry
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        MutableMapping,
    )
    from typing import Any

pytestmark = [pytest.mark.timeout(5)]


def _build_app(*, with_middleware: bool) -> FastAPI:
    """Build a FastAPI app whose handler resolves a RateLimiter ambiently."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
        async with micro:
            yield

    app = FastAPI(lifespan=lifespan)
    if with_middleware:
        app.add_middleware(GrelmicroMiddleware, micro=micro)

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
        result = await limiter.acquire(key="client")
        return {"allowed": result.allowed}

    return app


def test_middleware_binds_app_inside_request_handler() -> None:
    """A handler resolves the ambient backend when the middleware is present."""
    app = _build_app(with_middleware=True)
    with TestClient(app) as client:
        response = client.get("/limited")
    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}


def test_without_middleware_handler_misses_ambient_backend() -> None:
    """Without the middleware, the handler hits the ambient-miss guard."""
    app = _build_app(with_middleware=False)
    with TestClient(app) as client, pytest.raises(OutOfContextError):
        client.get("/limited")


async def test_middleware_passes_non_request_scopes_through() -> None:
    """The lifespan scope is forwarded untouched."""
    micro = Grelmicro()
    seen: list[str] = []

    async def downstream(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],  # noqa: ARG001
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],  # noqa: ARG001
    ) -> None:
        seen.append(str(scope["type"]))

    middleware = GrelmicroMiddleware(downstream, micro=micro)
    await middleware({"type": "lifespan"}, _noop_receive, _noop_send)
    assert seen == ["lifespan"]


async def test_middleware_resets_binding_after_request() -> None:
    """The current-app binding does not leak past the request scope."""
    micro = Grelmicro()
    from grelmicro._app import _current_micro  # noqa: PLC0415

    async def downstream(
        scope: MutableMapping[str, Any],  # noqa: ARG001
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],  # noqa: ARG001
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],  # noqa: ARG001
    ) -> None:
        assert _current_micro.get() is micro

    middleware = GrelmicroMiddleware(downstream, micro=micro)
    await middleware({"type": "http"}, _noop_receive, _noop_send)
    # Outside the request, the contextvar is restored to unset.
    with pytest.raises(LookupError):
        _current_micro.get()


async def _noop_receive() -> dict[str, object]:
    return {"type": "noop"}


async def _noop_send(message: object) -> None:  # noqa: ARG001
    return None
