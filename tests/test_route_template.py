"""The route a request took, read from its scope with `route_template`.

Each test builds an app, wires it with `micro.install(app)`, drives
requests through it, and reads what `route_template(scope)` returned from
a middleware of the app's own.
"""

from __future__ import annotations

import logging
import runpy
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from litestar import Litestar, Request
from litestar import Response as LitestarResponse
from litestar import asgi as litestar_asgi
from litestar import get as litestar_get
from litestar.exceptions import HTTPException
from litestar.middleware import DefineMiddleware
from litestar.params import Parameter
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

from grelmicro import Grelmicro, RouteReaderError
from grelmicro.http import AuthenticatedRequests, route_template
from grelmicro.log import AccessLog
from tests.test_authentication import verifier

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request as StarletteRequest
    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(10)]

Seen = list[str | RouteReaderError | None]
"""What each request read: the template, `None`, or the error raised."""

FRAMEWORKS = ["fastapi", "starlette", "litestar"]
STARLETTE_FAMILY = ["fastapi", "starlette"]
UNAUTHORIZED = 401
ACCESS_LOGGER = "grelmicro.access"
SNIPPET = Path(__file__).parent.parent / "docs/snippets/http/route_template.py"


def read(scope: Scope) -> str | RouteReaderError | None:
    """Return what `route_template` gives the scope, or the error it raised."""
    try:
        return route_template(scope)
    except RouteReaderError as error:
        return error


class Recorder:
    """Record the route template of each request, once the app answered."""

    def __init__(self, app: ASGIApp, seen: Seen) -> None:
        """Wrap `app`, recording into `seen`."""
        self.app = app
        self.seen = seen

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Run the app, then record the route its request took."""
        await self.app(scope, receive, send)
        if scope["type"] == "http":
            self.seen.append(read(scope))


class EarlyRecorder(Recorder):
    """Record the route template of each request, before the app runs."""

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Record the route, then run the app."""
        if scope["type"] == "http":
            self.seen.append(read(scope))
        await self.app(scope, receive, send)


async def ok(_request: StarletteRequest) -> PlainTextResponse:
    """Answer `200`."""
    return PlainTextResponse("ok")


def starlette_app(seen: Seen) -> Starlette:
    """Return a Starlette app serving items, under nested mounts too."""
    return Starlette(
        routes=[
            Route("/items/{item_id:int}", ok),
            Mount("/mm", app=Mount("/r", routes=[Route("/a", ok)])),
        ],
        middleware=[Middleware(Recorder, seen=seen)],
    )


def fastapi_app(seen: Seen) -> FastAPI:
    """Return a FastAPI app serving items, under nested mounts too."""
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> int:
        return item_id

    app.mount("/mm", Mount("/r", routes=[Route("/a", ok)]))
    app.add_middleware(Recorder, seen=seen)
    return app


def litestar_app(seen: Seen) -> Litestar:
    """Return a Litestar app serving items, under a mount too.

    Litestar runs an app's middleware only for a request a route took, so
    the app's exception handler records a request no route took.
    """

    @litestar_get("/items/{item_id:int}")
    async def item(item_id: Annotated[int, Parameter()]) -> int:
        return item_id

    @litestar_asgi("/mm", is_mount=True, copy_scope=False)
    async def mount(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await PlainTextResponse("mounted")(scope, receive, send)

    def refused(request: Request[Any, Any, Any], error: HTTPException) -> Any:  # noqa: ANN401
        seen.append(read(request.scope))  # ty: ignore[invalid-argument-type]
        return LitestarResponse(error.detail, status_code=error.status_code)

    return Litestar(
        route_handlers=[item, mount],
        middleware=[DefineMiddleware(cast("Any", Recorder), seen=seen)],
        exception_handlers={HTTPException: refused},
        logging_config=None,
    )


BUILDERS: dict[str, Callable[[Seen], Any]] = {
    "fastapi": fastapi_app,
    "starlette": starlette_app,
    "litestar": litestar_app,
}


def client_for(app: Any, root_path: str = "") -> httpx.AsyncClient:  # noqa: ANN401
    """Return a client that drives an ASGI app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path=root_path),
        base_url="http://probe",
    )


async def seen_for(
    framework: str,
    *paths: str,
    root_path: str = "",
    **options: Any,  # noqa: ANN401
) -> Seen:
    """Return what each request to the framework's installed app read."""
    seen: Seen = []
    app = BUILDERS[framework](seen)
    micro = Grelmicro(**options)
    micro.install(app)
    async with micro, client_for(app, root_path) as client:
        for path in paths:
            await client.get(path)
    return seen


# --- A route the request matched ------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_a_matched_route_reads_its_template(framework: str) -> None:
    """The route reads as its template, never the path."""
    assert await seen_for(framework, "/items/3") == ["/items/{item_id}"]


async def test_a_fastapi_route_reads_every_prefix_it_was_included_under() -> (
    None
):
    """A router included under a prefix reads its prefix and the include's."""
    seen: Seen = []
    recipes = APIRouter(prefix="/recipes")

    @recipes.get("/{recipe_id}")
    async def recipe(recipe_id: int) -> int:
        return recipe_id

    app = FastAPI()
    app.include_router(recipes, prefix="/api/meals/v1")
    app.add_middleware(Recorder, seen=seen)
    micro = Grelmicro()
    micro.install(app)

    async with micro, client_for(app) as client:
        await client.get("/api/meals/v1/recipes/7")

    assert seen == ["/api/meals/v1/recipes/{recipe_id}"]


async def test_a_route_under_nested_mounts_reads_every_mount() -> None:
    """A route under a mount of a mount joins both mounts."""
    assert await seen_for("starlette", "/mm/r/a") == ["/mm/r/a"]


# --- A URL no route matched -----------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_a_url_no_route_matches_reads_none(framework: str) -> None:
    """A URL nothing serves names no route."""
    assert await seen_for(framework, "/nowhere") == [None]


@pytest.mark.parametrize(
    ("framework", "path", "template"),
    [
        ("fastapi", "/mm/nowhere", "/mm/{path}"),
        ("starlette", "/mm/nowhere", "/mm/{path}"),
        ("starlette", "/mm/r/nowhere", "/mm/r/{path}"),
        ("litestar", "/mm/nowhere", "/mm/{path}"),
    ],
)
async def test_a_url_under_a_mount_reads_the_mount(
    framework: str, path: str, template: str
) -> None:
    """A URL under a mount reads as the template of the mount that took it."""
    assert await seen_for(framework, path) == [template]


# --- Behind a proxy -------------------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_the_root_path_prefixes_the_template(framework: str) -> None:
    """The template starts with the root path the app is served under."""
    seen = await seen_for(framework, "/proxy/items/3", root_path="/proxy")

    assert seen == ["/proxy/items/{item_id}"]


# --- A request answered before routing ------------------------------------


@pytest.mark.parametrize("framework", STARLETTE_FAMILY)
async def test_a_refused_request_reads_the_route_its_path_reaches(
    framework: str,
) -> None:
    """A request refused `401` before routing reads the route it would reach."""
    seen: Seen = []
    app = BUILDERS[framework](seen)
    micro = Grelmicro(uses=[AuthenticatedRequests(verifier())])
    micro.install(app)

    async with micro, client_for(app) as client:
        response = await client.get("/items/3")

    assert response.status_code == UNAUTHORIZED
    assert seen == ["/items/{item_id}"]


# --- Middleware the app adds ----------------------------------------------


class Routed(BaseHTTPMiddleware):
    """Record the route template once the rest of the app answered."""

    def __init__(self, app: ASGIApp, seen: Seen) -> None:
        """Wrap `app`, recording into `seen`."""
        super().__init__(app)
        self.seen = seen

    async def dispatch(
        self,
        request: StarletteRequest,
        call_next: Callable[[StarletteRequest], Awaitable[Response]],
    ) -> Response:
        """Answer, then record the route."""
        response = await call_next(request)
        self.seen.append(read(request.scope))
        return response


@pytest.mark.parametrize("framework", STARLETTE_FAMILY)
async def test_a_base_http_middleware_reads_the_route_after_call_next(
    framework: str,
) -> None:
    """A `BaseHTTPMiddleware` reads the route after `call_next`."""
    seen: Seen = []
    routed: Seen = []
    app = BUILDERS[framework](seen)
    app.add_middleware(Routed, seen=routed)
    micro = Grelmicro()
    micro.install(app)

    async with micro, client_for(app) as client:
        await client.get("/items/3")

    assert routed == seen == ["/items/{item_id}"]


# --- Where it cannot read -------------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_an_app_nothing_installed_raises(framework: str) -> None:
    """An app `micro.install` never wired raises `RouteReaderError`."""
    seen: Seen = []
    app = BUILDERS[framework](seen)

    async with client_for(app) as client:
        await client.get("/items/3")

    [error] = seen
    assert isinstance(error, RouteReaderError)
    assert isinstance(error, LookupError)
    assert "micro.install(app)" in str(error)


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_a_wrapper_outside_the_installed_app_raises(
    framework: str,
) -> None:
    """A wrapper reading before the installed app runs raises `RouteReaderError`."""
    inner: Seen = []
    outer: Seen = []
    app = BUILDERS[framework](inner)
    micro = Grelmicro()
    micro.install(app)

    async with micro, client_for(EarlyRecorder(app, outer)) as client:
        await client.get("/items/3")

    [error] = outer
    assert isinstance(error, RouteReaderError)
    assert inner == ["/items/{item_id}"]


# --- The same route the access log names ----------------------------------


@pytest.mark.parametrize(
    ("framework", "paths"),
    [
        (
            "fastapi",
            ["/items/3", "/items/x", "/nowhere", "/mm/r/a", "/mm/r/nowhere"],
        ),
        (
            "starlette",
            ["/items/3", "/items/x", "/nowhere", "/mm/r/a", "/mm/nowhere"],
        ),
        ("litestar", ["/items/3", "/items/x", "/nowhere", "/mm/anything"]),
    ],
)
@pytest.mark.parametrize("root_path", ["", "/proxy"])
async def test_it_reads_the_route_the_access_log_names(
    caplog: pytest.LogCaptureFixture,
    framework: str,
    paths: list[str],
    root_path: str,
) -> None:
    """Every request reads the route its access record names as `http.route`."""
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER)

    seen = await seen_for(
        framework,
        *(root_path + path for path in paths),
        root_path=root_path,
        uses=[AccessLog()],
    )

    logged = [
        record.__dict__.get("http.route")
        for record in caplog.records
        if record.name == ACCESS_LOGGER
    ]
    assert len(seen) == len(paths)
    assert seen == logged


# --- The documented example -----------------------------------------------


async def test_the_documented_example_records_the_full_template() -> None:
    """The documented middleware records the template with every prefix."""
    namespace = runpy.run_path(str(SNIPPET))

    async with namespace["micro"], client_for(namespace["app"]) as client:
        await client.get("/api/meals/v1/recipes/7")
        await client.get("/nowhere")

    assert namespace["seen"] == {"/api/meals/v1/recipes/{recipe_id}"}
