"""Tests for the Litestar integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import pytest
from litestar import Litestar, get
from litestar.middleware import DefineMiddleware
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
from litestar.testing import AsyncTestClient

from grelmicro import Grelmicro
from grelmicro.integrations.litestar import GrelmicroMiddleware, is_bound
from grelmicro.resilience import RateLimiter, RateLimiterComponent
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

pytestmark = [pytest.mark.timeout(5)]


@get("/limited")
async def limited() -> dict[str, bool]:
    """Resolve a rate limiter ambiently, with no explicit backend."""
    limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
    result = await limiter.acquire(key="client")
    return {"allowed": result.allowed}


def _build_app(
    *, events: list[str] | None = None
) -> tuple[Litestar, Grelmicro]:
    """Build a Litestar app and the `Grelmicro` app to install into it."""
    micro = Grelmicro(uses=[RateLimiterComponent(MemoryRateLimiterAdapter())])

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:  # noqa: ARG001
        if events is not None:
            events.append("enter")
        yield
        if events is not None:
            events.append("exit")

    return Litestar(route_handlers=[limited], lifespan=[lifespan]), micro


async def test_install_wires_lifecycle_and_binding() -> None:
    """`micro.install(app)` opens micro and binds it inside a handler."""
    app, micro = _build_app()
    micro.install(app)

    async with AsyncTestClient(app=app) as client:
        response = await client.get("/limited")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"allowed": True}


async def test_install_keeps_an_existing_lifespan() -> None:
    """A lifespan already passed to Litestar keeps running."""
    events: list[str] = []
    app, micro = _build_app(events=events)
    micro.install(app)

    async with AsyncTestClient(app=app) as client:
        response = await client.get("/limited")

    assert response.status_code == HTTP_200_OK
    assert events == ["enter", "exit"]


async def test_install_ambient_false_skips_the_binding() -> None:
    """`ambient=False` opens micro but does not bind it per request."""
    app, micro = _build_app()
    with pytest.warns(UserWarning, match="ambient=False"):
        micro.install(app, ambient=False)

    assert not micro.check_ambient_binding(app)
    async with AsyncTestClient(app=app) as client:
        response = await client.get("/limited")

    assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR


async def test_check_ambient_binding_reports_the_install() -> None:
    """`check_ambient_binding` is False before install and True after."""
    app, micro = _build_app()

    assert not micro.check_ambient_binding(app)
    micro.install(app)
    assert micro.check_ambient_binding(app)


def test_is_bound_finds_middleware_passed_to_the_constructor() -> None:
    """A `DefineMiddleware` entry counts as bound, so install does not wrap it twice."""
    micro = Grelmicro(uses=[RateLimiterComponent(MemoryRateLimiterAdapter())])
    app = Litestar(
        route_handlers=[limited],
        middleware=[DefineMiddleware(GrelmicroMiddleware, micro=micro)],  # ty: ignore[invalid-argument-type]
    )

    assert is_bound(app)

    handler = app.asgi_handler
    micro.install(app)

    assert app.asgi_handler is handler


async def test_shutdown_before_startup_does_not_raise() -> None:
    """A shutdown hook that runs without a successful startup closes nothing.

    Litestar registers the shutdown callbacks before it enters the startup
    hooks, so one runs after a failed `__aenter__` left micro unopened.
    """
    app, micro = _build_app()
    micro.install(app)

    close = cast("Callable[[], Awaitable[None]]", app.on_shutdown[0])
    await close()
