"""The route a request took, read from its scope with `route_template`.

Each test builds an app, wires it with `micro.install(app)`, drives
requests through it, and reads what `route_template(scope)` returned from
a middleware of the app's own, or from a wrapper around the app.
"""

from __future__ import annotations

import logging
import runpy
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi import Request as FastAPIRequest
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

from grelmicro import (
    AppNotInstalledError,
    Grelmicro,
    MiddlewarePlacementWarning,
)
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    AuthenticatedRequestsMiddleware,
    CachedResponses,
    route_template,
)
from grelmicro.log import AccessLog
from tests.test_authentication import verifier

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request as StarletteRequest
    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(10)]

Seen = list[str | AppNotInstalledError | None]
"""What each request read: the template, `None`, or the error raised."""

FRAMEWORKS = ["fastapi", "starlette", "litestar"]
STARLETTE_FAMILY = ["fastapi", "starlette"]
OK = 200
UNAUTHORIZED = 401
ACCESS_LOGGER = "grelmicro.access"
SNIPPET = Path(__file__).parent.parent / "docs/snippets/http/route_template.py"


def read(scope: Scope) -> str | AppNotInstalledError | None:
    """Return what `route_template` gives the scope, or the error it raised."""
    try:
        return route_template(scope)
    except AppNotInstalledError as error:
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


def installed(
    framework: str,
    **options: Any,  # noqa: ANN401
) -> tuple[Any, Grelmicro, Seen]:
    """Return the framework's app, installed, and what its requests read."""
    seen: Seen = []
    app = BUILDERS[framework](seen)
    micro = Grelmicro(**options)
    micro.install(app)
    return app, micro, seen


async def drive(
    micro: Grelmicro | None,
    app: Any,  # noqa: ANN401
    *paths: str,
    root_path: str = "",
) -> list[httpx.Response]:
    """Send a `GET` to each path, with `micro` running when given."""
    async with client_for(app, root_path) as client:
        if micro is None:
            return [await client.get(path) for path in paths]
        async with micro:
            return [await client.get(path) for path in paths]


# --- A route the request matched ------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_route_template_matched_route_returns_its_template(
    framework: str,
) -> None:
    """The route reads as its template, never the path."""
    # Arrange
    app, micro, seen = installed(framework)
    # Act
    await drive(micro, app, "/items/3")
    # Assert
    assert seen == ["/items/{item_id}"]


async def test_route_template_included_router_returns_every_prefix() -> None:
    """A router included under a prefix reads its prefix and the include's."""
    # Arrange
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
    # Act
    await drive(micro, app, "/api/meals/v1/recipes/7")
    # Assert
    assert seen == ["/api/meals/v1/recipes/{recipe_id}"]


async def test_route_template_nested_starlette_mounts_returns_every_mount() -> (
    None
):
    """A route under a mount of a mount joins both mounts."""
    # Arrange
    app, micro, seen = installed("starlette")
    # Act
    await drive(micro, app, "/mm/r/a")
    # Assert
    assert seen == ["/mm/r/a"]


async def test_route_template_fastapi_sub_app_returns_its_route() -> None:
    """A route of a FastAPI app mounted in FastAPI reads its own template."""
    # Arrange
    seen: Seen = []
    sub = FastAPI()

    @sub.get("/r/{x}")
    async def route(x: int) -> int:
        return x

    app = FastAPI()
    app.mount("/mm", sub)
    app.add_middleware(Recorder, seen=seen)
    micro = Grelmicro()
    micro.install(app)
    # Act
    await drive(micro, app, "/mm/r/1")
    # Assert
    assert seen == ["/mm/r/{x}"]


@pytest.mark.parametrize(
    "mounted",
    [
        Starlette(routes=[Route("/r/{x}", ok)]),
        Mount("/r", routes=[Route("/{x}", ok)]),
    ],
    ids=["starlette-app", "starlette-mount"],
)
async def test_route_template_starlette_mounted_in_fastapi_returns_the_mount(
    mounted: ASGIApp,
) -> None:
    """A Starlette app or `Mount` mounted in FastAPI reads as the mount."""
    # Arrange
    seen: Seen = []
    app = FastAPI()
    app.mount("/mm", mounted)
    app.add_middleware(Recorder, seen=seen)
    micro = Grelmicro()
    micro.install(app)
    # Act
    [response] = await drive(micro, app, "/mm/r/1")
    # Assert
    assert response.status_code == OK
    assert seen == ["/mm/{path}"]


# --- A URL no route matched -----------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_route_template_returns_none_when_no_route_matches(
    framework: str,
) -> None:
    """A URL nothing serves names no route."""
    # Arrange
    app, micro, seen = installed(framework)
    # Act
    await drive(micro, app, "/nowhere")
    # Assert
    assert seen == [None]


@pytest.mark.parametrize(
    ("framework", "path", "template"),
    [
        ("fastapi", "/mm/nowhere", "/mm/{path}"),
        ("starlette", "/mm/nowhere", "/mm/{path}"),
        ("starlette", "/mm/r/nowhere", "/mm/r/{path}"),
        ("litestar", "/mm/nowhere", "/mm/{path}"),
    ],
)
async def test_route_template_unmatched_url_under_mount_returns_the_mount(
    framework: str, path: str, template: str
) -> None:
    """A URL under a mount reads as the template of the mount that took it."""
    # Arrange
    app, micro, seen = installed(framework)
    # Act
    await drive(micro, app, path)
    # Assert
    assert seen == [template]


# --- Behind a proxy -------------------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_route_template_root_path_prefixes_the_template(
    framework: str,
) -> None:
    """The template starts with the root path the app is served under."""
    # Arrange
    app, micro, seen = installed(framework)
    # Act
    await drive(micro, app, "/proxy/items/3", root_path="/proxy")
    # Assert
    assert seen == ["/proxy/items/{item_id}"]


# --- A request answered before routing ------------------------------------


@pytest.mark.parametrize("framework", STARLETTE_FAMILY)
async def test_route_template_refused_request_returns_the_route_it_reaches(
    framework: str,
) -> None:
    """A request refused `401` before routing reads the route it would reach."""
    # Arrange
    app, micro, seen = installed(
        framework, uses=[AuthenticatedRequests(verifier())]
    )
    # Act
    [response] = await drive(micro, app, "/items/3")
    # Assert
    assert response.status_code == UNAUTHORIZED
    assert seen == ["/items/{item_id}"]


def bare_litestar_app(_seen: Seen) -> Litestar:
    """Return a Litestar app serving items, with no middleware of its own."""

    @litestar_get("/items/{item_id:int}")
    async def item(item_id: Annotated[int, Parameter()]) -> int:
        return item_id

    return Litestar(route_handlers=[item], logging_config=None)


@pytest.mark.parametrize(
    "build", [fastapi_app, starlette_app, bare_litestar_app]
)
async def test_route_template_refused_request_read_outside_returns_the_route(
    build: Callable[[Seen], Any],
) -> None:
    """A wrapper reading a refused request after the installed app ran reads its route."""
    # Arrange
    outer: Seen = []
    app = build([])
    micro = Grelmicro(uses=[AuthenticatedRequests(verifier())])
    micro.install(app)
    # Act
    [response] = await drive(micro, Recorder(app, outer), "/items/3")
    # Assert
    assert response.status_code == UNAUTHORIZED
    assert outer == ["/items/{item_id}"]


async def test_route_template_litestar_refused_request_unseen_by_its_middleware() -> (
    None
):
    """Litestar's own middleware never sees a request refused before routing."""
    # Arrange
    inner: Seen = []
    outer: Seen = []
    app: Any = litestar_app(inner)
    micro = Grelmicro(uses=[AuthenticatedRequests(verifier())])
    with pytest.warns(MiddlewarePlacementWarning):
        micro.install(app)
    # Act
    [response] = await drive(micro, Recorder(app, outer), "/items/3")
    # Assert
    assert response.status_code == UNAUTHORIZED
    assert inner == []
    assert outer == ["/items/{item_id}"]


async def test_route_template_cached_response_returns_the_route() -> None:
    """A response the cache answers reads the route its path reaches."""
    # Arrange
    seen: Seen = []
    served: list[int] = []
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> int:
        served.append(item_id)
        return item_id

    app.add_middleware(Recorder, seen=seen)
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/items/*": 60}),
        ]
    )
    micro.install(app)
    # Act
    responses = await drive(micro, app, "/items/3", "/items/3")
    # Assert
    assert [response.status_code for response in responses] == [OK, OK]
    assert served == [3]
    assert seen == ["/items/{item_id}"] * 2


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
async def test_route_template_base_http_middleware_returns_the_route(
    framework: str,
) -> None:
    """A `BaseHTTPMiddleware` reads the route after `call_next`."""
    # Arrange
    routed: Seen = []
    seen: Seen = []
    app = BUILDERS[framework](seen)
    app.add_middleware(Routed, seen=routed)
    micro = Grelmicro()
    micro.install(app)
    # Act
    await drive(micro, app, "/items/3")
    # Assert
    assert routed == seen == ["/items/{item_id}"]


# --- Where it cannot read -------------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_route_template_raises_when_app_not_installed(
    framework: str,
) -> None:
    """An app `micro.install` never wired raises `AppNotInstalledError`."""
    # Arrange
    seen: Seen = []
    app = BUILDERS[framework](seen)
    # Act
    await drive(None, app, "/items/3")
    # Assert
    [error] = seen
    assert isinstance(error, AppNotInstalledError)
    assert isinstance(error, LookupError)
    assert "micro.install(app)" in str(error)


@pytest.mark.parametrize("framework", STARLETTE_FAMILY)
async def test_route_template_raises_when_hand_added_middleware_refused_it(
    framework: str,
) -> None:
    """A request an uninstalled app's own authentication refused raises."""
    # Arrange
    outer: Seen = []
    app = BUILDERS[framework]([])
    app.add_middleware(AuthenticatedRequestsMiddleware, verifier=verifier())
    # Act
    [response] = await drive(None, Recorder(app, outer), "/items/3")
    # Assert
    assert response.status_code == UNAUTHORIZED
    [error] = outer
    assert isinstance(error, AppNotInstalledError)


@pytest.mark.parametrize("framework", FRAMEWORKS)
async def test_route_template_raises_when_read_before_installed_app_runs(
    framework: str,
) -> None:
    """A wrapper reading before the installed app runs raises `AppNotInstalledError`."""
    # Arrange
    outer: Seen = []
    app, micro, inner = installed(framework)
    # Act
    await drive(micro, EarlyRecorder(app, outer), "/items/3")
    # Assert
    [error] = outer
    assert isinstance(error, AppNotInstalledError)
    assert inner == ["/items/{item_id}"]


# --- The same route the access log names ----------------------------------


@pytest.mark.parametrize(
    ("framework", "expected"),
    [("fastapi", "/items/{item_id}"), ("starlette", None)],
)
async def test_route_template_slash_redirect_returns_its_target_on_fastapi_only(
    caplog: pytest.LogCaptureFixture,
    framework: str,
    expected: str | None,
) -> None:
    """A slash redirect names its target on FastAPI, and no route on Starlette.

    The access log names the target on both.
    """
    # Arrange
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER)
    app, micro, seen = installed(framework, uses=[AccessLog()])
    # Act
    responses = await drive(micro, app, "/items/3/")
    # Assert
    logged = [
        record.__dict__.get("http.route")
        for record in caplog.records
        if record.name == ACCESS_LOGGER
    ]
    assert responses[0].status_code == httpx.codes.TEMPORARY_REDIRECT
    assert seen == [expected]
    assert logged == ["/items/{item_id}"]


@pytest.mark.parametrize("framework", STARLETTE_FAMILY)
async def test_route_template_handler_returns_its_route(framework: str) -> None:
    """A handler reads the route it serves."""
    # Arrange
    seen: Seen = []

    async def handler(request: FastAPIRequest) -> PlainTextResponse:
        seen.append(read(request.scope))
        return PlainTextResponse("ok")

    if framework == "fastapi":
        app: Any = FastAPI()
        app.add_api_route("/items/{item_id}", handler)
    else:
        app = Starlette(routes=[Route("/items/{item_id}", handler)])
    micro = Grelmicro()
    micro.install(app)
    # Act
    await drive(micro, app, "/items/3")
    # Assert
    assert seen == ["/items/{item_id}"]


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
async def test_route_template_returns_the_route_the_access_log_names(
    caplog: pytest.LogCaptureFixture,
    framework: str,
    paths: list[str],
    root_path: str,
) -> None:
    """Every request reads the route its access record names as `http.route`."""
    # Arrange
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER)
    app, micro, seen = installed(framework, uses=[AccessLog()])
    # Act
    await drive(
        micro,
        app,
        *(root_path + path for path in paths),
        root_path=root_path,
    )
    # Assert
    logged = [
        record.__dict__.get("http.route")
        for record in caplog.records
        if record.name == ACCESS_LOGGER
    ]
    assert len(seen) == len(paths)
    assert seen == logged


# --- The documented example -----------------------------------------------


async def test_route_template_documented_example_records_the_template() -> None:
    """The documented middleware records the template with every prefix."""
    # Arrange
    namespace = runpy.run_path(str(SNIPPET))
    # Act
    await drive(
        namespace["micro"],
        namespace["app"],
        "/api/meals/v1/recipes/7",
        "/nowhere",
    )
    # Assert
    assert namespace["seen"] == {"/api/meals/v1/recipes/{recipe_id}"}
