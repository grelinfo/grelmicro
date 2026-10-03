"""Tests for the Litestar integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from litestar import Litestar, asgi, get, post, put
from litestar.config.cors import CORSConfig
from litestar.exceptions import HTTPException
from litestar.middleware import DefineMiddleware
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_409_CONFLICT,
    HTTP_428_PRECONDITION_REQUIRED,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
from litestar.testing import AsyncTestClient, TestClient

from grelmicro import Grelmicro, GrelmicroMiddleware
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    CachedResponses,
    ConditionalRequests,
    ErrorResponses,
    IdempotentRequests,
    RateLimitedRequests,
)
from grelmicro.integrations.litestar import is_bound
from grelmicro.resilience import RateLimiter, RateLimiterComponent
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security import TrustedProxies
from tests.test_route_gate_litestar import Passing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from litestar.types import Receive, Scope, Send

pytestmark = [pytest.mark.timeout(5)]


@get("/limited")
async def limited() -> dict[str, bool]:
    """Resolve a rate limiter ambiently, with no explicit backend."""
    limiter = RateLimiter.sliding_window("api", limit=10, window=1)
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

    micro.install(app)

    layers = []
    handler = app.asgi_handler
    while handler is not None and handler is not app and callable(handler):
        layers.append(handler)
        handler = getattr(handler, "app", None)
    assert not any(isinstance(layer, GrelmicroMiddleware) for layer in layers)


async def test_shutdown_before_startup_does_not_raise() -> None:
    """A shutdown hook that runs without a successful startup closes nothing.

    Litestar registers the shutdown callbacks before it enters the startup
    hooks, so one runs after a failed `__aenter__` left micro unopened.
    """
    app, micro = _build_app()
    micro.install(app)

    close = cast("Callable[[], Awaitable[None]]", app.on_shutdown[0])
    await close()


def test_answering_middleware_nests_in_registration_order() -> None:
    """The first one registered answers first, as it does on Starlette."""
    limiter = RateLimiter.sliding_window(
        "burst", limit=10, window=60, backend=MemoryRateLimiterAdapter()
    )
    app = Litestar(route_handlers=[limited])
    Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                limiter, trusted=TrustedProxies(["10.0.0.0/8"])
            ),
            CachedResponses(include=("/limited",)),
        ]
    ).install(app)

    chain: list[str] = []
    handler: object = app.asgi_handler
    while handler is not None and handler is not app:
        chain.append(type(handler).__name__)
        handler = getattr(handler, "app", None)

    assert chain.index("RateLimitMiddleware") < chain.index(
        "CachedResponsesMiddleware"
    )


def _under_outer_app(inner: Litestar, *, outer_installed: bool) -> Litestar:
    """Return an outer Litestar app serving `inner` under the mount `/in`."""

    @asgi("/in", is_mount=True, copy_scope=False)
    async def mount(scope: Scope, receive: Receive, send: Send) -> None:
        await inner(scope, receive, send)

    outer = Litestar([mount])
    if outer_installed:
        Grelmicro(uses=[ErrorResponses()]).install(outer)
    return outer


@pytest.mark.parametrize("outer_installed", [False, True])
def test_a_mounted_app_replays_a_write_matched_on_its_own_path(
    *, outer_installed: bool
) -> None:
    """Idempotency on an app mounted under another Litestar app matches `/write`."""
    served: list[str] = []

    @post("/write")
    async def write() -> str:
        served.append("write")
        return "written"

    inner = Litestar([write])
    Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(include=("/write",), requires="process"),
        ]
    ).install(inner)
    outer = _under_outer_app(inner, outer_installed=outer_installed)
    key = {"Idempotency-Key": "signup-1"}
    with TestClient(inner), TestClient(outer) as client:
        responses = [client.post("/in/write", headers=key) for _ in range(2)]

    assert [response.text for response in responses] == ["written"] * 2
    assert served == ["write"]


@pytest.mark.parametrize("outer_installed", [False, True])
def test_a_mounted_app_caches_a_read_matched_on_its_own_path(
    *, outer_installed: bool
) -> None:
    """The response cache on an app mounted under another Litestar app matches `/read`."""
    served: list[str] = []

    @get("/read")
    async def read() -> str:
        served.append("read")
        return "fresh"

    inner = Litestar([read])
    Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/read": 60}),
        ]
    ).install(inner)
    outer = _under_outer_app(inner, outer_installed=outer_installed)
    with TestClient(inner), TestClient(outer) as client:
        responses = [client.get("/in/read") for _ in range(2)]

    assert [response.text for response in responses] == ["fresh"] * 2
    assert served == ["read"]


@post("/conflict")
async def conflict() -> None:
    """Refuse the request with a `409` the app chose."""
    raise HTTPException(status_code=HTTP_409_CONFLICT)


@pytest.mark.filterwarnings("ignore::grelmicro.MiddlewarePlacementWarning")
@pytest.mark.parametrize(
    ("middleware", "registered_later"),
    [
        pytest.param([], False, id="no app middleware"),
        pytest.param([Passing], False, id="app middleware"),
        pytest.param([], True, id="registered after install"),
    ],
)
def test_a_raised_http_exception_passes_through_our_middleware_as_a_response(
    middleware: list[Any], *, registered_later: bool
) -> None:
    """A route's `HTTPException` states the rate limit and keeps its CORS fields.

    Litestar renders it inside the route, under every middleware of ours,
    whether or not the app declares middleware of its own.
    """
    # Arrange
    limiter = RateLimiter.sliding_window(
        "conflict", limit=10, window=60, backend=MemoryRateLimiterAdapter()
    )
    app = Litestar(
        [] if registered_later else [conflict],
        middleware=middleware,
        cors_config=CORSConfig(allow_origins=["*"]),
    )
    Grelmicro(
        uses=[
            RateLimitedRequests(
                limiter,
                key=lambda scope: "one caller",  # noqa: ARG005
            )
        ]
    ).install(app)
    if registered_later:
        app.register(conflict)

    # Act
    with TestClient(app) as client:
        response = client.post(
            "/conflict", headers={"Origin": "https://example.com"}
        )

    # Assert
    assert response.status_code == HTTP_409_CONFLICT
    assert "ratelimit" in response.headers
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("outer_installed", [False, True])
def test_a_mounted_app_rate_limits_a_route_matched_on_its_own_path(
    *, outer_installed: bool
) -> None:
    """The rate limit on an app mounted under another Litestar app matches `/limited`."""

    @get("/limited")
    async def once() -> str:
        return "served"

    limiter = RateLimiter.sliding_window(
        "once", limit=1, window=60, backend=MemoryRateLimiterAdapter()
    )
    inner = Litestar([once])
    Grelmicro(
        uses=[
            RateLimitedRequests(
                limiter,
                include=("/limited",),
                key=lambda scope: "one caller",  # noqa: ARG005
            )
        ]
    ).install(inner)
    outer = _under_outer_app(inner, outer_installed=outer_installed)
    with TestClient(inner), TestClient(outer) as client:
        statuses = [client.get("/in/limited").status_code for _ in range(2)]

    assert statuses == [HTTP_200_OK, HTTP_429_TOO_MANY_REQUESTS]


@pytest.mark.parametrize("outer_installed", [False, True])
def test_a_mounted_app_requires_a_precondition_matched_on_its_own_path(
    *, outer_installed: bool
) -> None:
    """Conditional requests on an app mounted under another Litestar app match `/doc`."""

    @put("/doc")
    async def replace() -> str:
        return "replaced"  # pragma: no cover

    inner = Litestar([replace])
    Grelmicro(
        uses=[
            ConditionalRequests(
                require_precondition=("PUT",), include=("/doc",)
            )
        ]
    ).install(inner)
    outer = _under_outer_app(inner, outer_installed=outer_installed)
    with TestClient(inner), TestClient(outer) as client:
        response = client.put("/in/doc")

    assert response.status_code == HTTP_428_PRECONDITION_REQUIRED
