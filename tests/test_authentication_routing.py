"""Authentication decides from the declaration of the route the framework routed to.

No route is read before routing. An integration declares its routes through
`install_route_gate` and `route_declarations`, and one without them declares
nothing, so every route needs a credential. The first half drives `tinyweb`, a small
framework of our own published through the `grelmicro.integrations` entry
point, whose middleware chain sits in an attribute only it knows. The second
runs rows of the authentication case table for the URLs a router emulation
used to guess at: converters, a path spanning segments, a host, a trailing
slash and a route added once the app was installed.
"""

from __future__ import annotations

import importlib.metadata
import warnings
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from litestar import Litestar, get
from litestar.params import FromPath
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Host, Route

from grelmicro import (
    Grelmicro,
    GrelmicroConfigWarning,
    MiddlewarePlacementError,
    RoutesNotDeclaredWarning,
)
from grelmicro._discovery import INTEGRATION_GROUP
from grelmicro.http import (
    AuthenticatedRequests,
    ErrorResponses,
    RouteDeclaration,
)
from grelmicro.integrations.fastapi import Anonymous
from grelmicro.integrations.litestar import Anonymous as LitestarAnonymous
from grelmicro.testing import FakeVerifier, fake_claims
from tests.test_authentication_cases import Case, answered
from tests.test_authentication_cases import (
    authenticated as table_authentication,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(5)]

TOKEN = {"authorization": "Bearer alice"}
"""A credential the verifier accepts."""

OK = 200
UNAUTHORIZED = 401
NOT_FOUND = 404


def authenticated(**options: Any) -> AuthenticatedRequests:  # noqa: ANN401
    """Return the authentication every app here installs."""
    return AuthenticatedRequests(
        FakeVerifier(alice=fake_claims("alice")), **options
    )


@dataclass
class TinyRoute:
    """One route: a path matched exactly, and the handler it dispatches to."""

    path: str
    anonymous: bool = False
    app: ASGIApp = field(init=False)

    def __post_init__(self) -> None:
        """Dispatch to a handler answering `200`."""
        self.app = PlainTextResponse("served")


class TinyApp:
    """A framework whose middleware chain starts at `entry`, an attribute only it knows."""

    def __init__(self, *routes: TinyRoute) -> None:
        """Route to `routes` by exact path, answering `404` to any other."""
        self.routes = list(routes)
        self.entry: ASGIApp = self._route
        self.state = SimpleNamespace()

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Name the app serving the request, then run its middleware."""
        scope["app"] = self
        await self.entry(scope, receive, send)

    async def _route(self, scope: Scope, receive: Receive, send: Send) -> None:
        for route in self.routes:
            if route.path == scope["path"]:
                await route.app(scope, receive, send)
                return
        await PlainTextResponse("nowhere", NOT_FOUND)(scope, receive, send)


TinyApp.__module__ = "tinyweb"


def _install(app: TinyApp, micro: Grelmicro, *, ambient: bool = True) -> None:
    """Open nothing: the tests serve no lifespan."""


def _install_middleware(app: TinyApp, components: list[Any]) -> None:
    """Wrap the chain in each component's middleware, the first outermost."""
    for component in reversed(components):
        middleware, options = component.asgi_middleware()
        app.entry = middleware(app.entry, **options)


def _declare(route: TinyRoute) -> RouteDeclaration:
    return RouteDeclaration(
        route.path, methods=frozenset({"GET"}), anonymous=route.anonymous
    )


def _route_declarations(app: TinyApp) -> list[RouteDeclaration]:
    return [_declare(route) for route in app.routes]


def _install_route_gate(app: TinyApp, gate: Any) -> None:  # noqa: ANN401
    for route in app.routes:
        route.app = gate(route.app, _declare(route))


hookless = SimpleNamespace(
    install=_install,
    is_bound=lambda app: True,  # noqa: ARG005
    install_middleware=_install_middleware,
)
"""An integration published before the route hooks existed."""

hooked = SimpleNamespace(
    install=_install,
    is_bound=lambda app: True,  # noqa: ARG005
    install_middleware=_install_middleware,
    install_route_gate=_install_route_gate,
    route_declarations=_route_declarations,
)
"""The same integration, declaring its routes."""

unplaced = SimpleNamespace(
    install=_install,
    is_bound=lambda app: True,  # noqa: ARG005
    install_route_gate=_install_route_gate,
    route_declarations=_route_declarations,
)
"""An integration declaring its routes, with nothing to add a middleware with."""

bare = SimpleNamespace(
    install=_install,
    is_bound=lambda app: True,  # noqa: ARG005
)
"""An integration with nothing but `install` and `is_bound`."""

listing = SimpleNamespace(
    install=_install,
    is_bound=lambda app: True,  # noqa: ARG005
    install_middleware=_install_middleware,
    route_declarations=_route_declarations,
)
"""An integration listing its routes without gating them."""

offline = SimpleNamespace(
    install=_install,
    is_bound=lambda app: True,  # noqa: ARG005
    health_endpoints=lambda app: None,  # noqa: ARG005
)
"""An integration whose app serves no HTTP."""


def publish(monkeypatch: pytest.MonkeyPatch, integration: str) -> None:
    """Register `integration` of this module for `tinyweb` apps."""
    published = importlib.metadata.EntryPoint(
        "tinyweb", f"{__name__}:{integration}", INTEGRATION_GROUP
    )
    found = importlib.metadata.entry_points

    def entry_points(*, group: str) -> list[importlib.metadata.EntryPoint]:
        listed = list(found(group=group))
        return [*listed, published] if group == INTEGRATION_GROUP else listed

    monkeypatch.setattr("grelmicro._discovery.entry_points", entry_points)


def catalog() -> TinyApp:
    """Return an app with a public and a protected route."""
    return TinyApp(TinyRoute("/catalog", anonymous=True), TinyRoute("/orders"))


def test_authenticated_requests_integration_without_hooks_warns_at_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report names the missing hook and what to ship."""
    # Arrange
    publish(monkeypatch, "hookless")
    micro = Grelmicro(uses=[ErrorResponses(), authenticated()])

    # Act / Assert
    with pytest.warns(
        RoutesNotDeclaredWarning,
        match=(
            r"tinyweb app whose integration has no install_route_gate.*"
            r"Ship install_route_gate and route_declarations.*"
            r"\[routes-not-declared\]"
        ),
    ):
        micro.install(catalog())


def test_authenticated_requests_integration_without_hooks_requires_a_credential_on_every_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route the framework marks public is refused without one, and served with one."""
    # Arrange
    publish(monkeypatch, "hookless")
    app = catalog()
    with pytest.warns(RoutesNotDeclaredWarning):
        Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)
    client = TestClient(app)

    # Act
    public = client.get("/catalog")
    protected = client.get("/orders")
    nowhere = client.get("/nowhere")
    with_token = client.get("/catalog", headers=TOKEN)

    # Assert
    assert public.status_code == UNAUTHORIZED
    assert protected.status_code == UNAUTHORIZED
    assert nowhere.status_code == UNAUTHORIZED
    assert with_token.status_code == OK


def test_authenticated_requests_integration_without_hooks_describes_every_route_as_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no declaration, the report reads no route as public."""
    # Arrange
    publish(monkeypatch, "hookless")
    app = catalog()
    micro = Grelmicro(uses=[ErrorResponses(), authenticated()])
    with pytest.warns(RoutesNotDeclaredWarning):
        micro.install(app)

    # Act
    endpoints = micro.describe(app).endpoints

    # Assert
    assert endpoints == ()


def test_authenticated_requests_integration_with_hooks_serves_its_anonymous_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate of the route the framework routed to decides, after routing.

    The core never reads the middleware chain: `install_middleware` placed
    the authentication.
    """
    # Arrange
    publish(monkeypatch, "hooked")
    app = catalog()
    Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)
    client = TestClient(app)
    assert not hasattr(app, "asgi_handler")
    assert not hasattr(app, "user_middleware")

    # Act
    public = client.get("/catalog")
    protected = client.get("/orders")
    nowhere = client.get("/nowhere")
    with_token = client.get("/orders", headers=TOKEN)

    # Assert
    assert public.status_code == OK
    assert protected.status_code == UNAUTHORIZED
    assert nowhere.status_code == UNAUTHORIZED
    assert with_token.status_code == OK


@pytest.mark.parametrize("integration", ["unplaced", "bare"])
def test_authenticated_requests_integration_without_install_middleware_fails_install(
    monkeypatch: pytest.MonkeyPatch, integration: str
) -> None:
    """Nothing would authenticate a request, so the app is refused unwired."""
    # Arrange
    publish(monkeypatch, integration)
    app = catalog()
    micro = Grelmicro(uses=[ErrorResponses(), authenticated()])

    # Act / Assert
    with pytest.raises(
        MiddlewarePlacementError,
        match=(
            r"integration for tinyweb has no install_middleware.*"
            r"Ship install_middleware, install_route_gate and "
            r"route_declarations.*\[middleware-placement\]"
        ),
    ):
        micro.install(app)
    assert TestClient(app).get("/catalog").status_code == OK


def test_authenticated_requests_integration_serving_no_http_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app whose integration says it serves no HTTP is not refused."""
    # Arrange
    publish(monkeypatch, "offline")
    micro = Grelmicro(uses=[ErrorResponses(), authenticated()])

    # Act / Assert: nothing is refused.
    micro.install(catalog())


def test_authenticated_requests_integration_listing_without_gating_refuses_every_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `install_route_gate` the listing gates nothing, and is reported as such."""
    # Arrange
    publish(monkeypatch, "listing")
    app = catalog()
    with pytest.warns(RoutesNotDeclaredWarning, match="no install_route_gate"):
        Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)
    client = TestClient(app)

    # Act
    public = client.get("/catalog")
    with_token = client.get("/catalog", headers=TOKEN)

    # Assert
    assert public.status_code == UNAUTHORIZED
    assert with_token.status_code == OK


def test_authenticated_requests_integration_with_hooks_installs_without_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An integration that declares its routes is not reported."""
    # Arrange
    publish(monkeypatch, "hooked")
    micro = Grelmicro(uses=[ErrorResponses(), authenticated()])

    # Act
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        micro.install(catalog())

    # Assert
    assert not [
        warning
        for warning in caught
        if issubclass(warning.category, GrelmicroConfigWarning)
    ]


def test_authenticated_requests_integration_without_hooks_is_not_reported_without_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is authenticated, so nothing is refused."""
    # Arrange
    publish(monkeypatch, "hookless")
    app = catalog()

    # Act
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Assert
    assert TestClient(app).get("/catalog").status_code == OK


ROUTED = (
    Case(
        "int converter, public",
        "/numbers/7",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "int converter, a value it refuses",
        "/numbers/seven",
        fastapi=(401, 404),
        starlette=(404, 404),
        litestar=(401, 404),
    ),
    Case(
        "path converter spanning segments, public",
        "/files/a/b/c.txt",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "trailing slash to a public route with a parameter",
        "/numbers/7/",
        fastapi=(401, 307),
        starlette=(307, 307),
        litestar=(200, 200),
    ),
    Case(
        "public route added after install",
        "/late/public",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "protected route added after install",
        "/late/private",
        fastapi=(401, 200),
        starlette=(401, 200),
        litestar=(401, 200),
    ),
)
"""Rows every framework answers. Starlette's public routes are the paths in `exclude=`."""

HOSTED = (
    Case(
        "public route under a host",
        "/hosted",
        headers={"host": "api.example"},
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=cast("Any", None),
    ),
    Case(
        "public route under a host, another host",
        "/hosted",
        headers={"host": "other.example"},
        fastapi=(401, 404),
        starlette=(404, 404),
        litestar=cast("Any", None),
    ),
)
"""Rows for the frameworks that route by host. Litestar does not."""


async def answer(request: Request) -> JSONResponse:
    """Answer with the path the request arrived at."""
    return JSONResponse({"path": request.url.path})


async def served() -> dict[str, bool]:
    """Answer on a FastAPI route."""
    return {"served": True}


def fastapi_app() -> FastAPI:
    """Return a FastAPI app declaring each public route with `Anonymous()`."""
    app = FastAPI()
    public = [Anonymous()]
    app.add_api_route("/numbers/{number:int}", served, dependencies=public)
    app.add_api_route("/files/{name:path}", served, dependencies=public)
    hosted = FastAPI()
    hosted.add_api_route("/hosted", served, dependencies=public)
    app.router.routes.append(Host("api.example", hosted))
    Grelmicro(uses=[ErrorResponses(), table_authentication()]).install(app)
    app.add_api_route("/late/public", served, dependencies=public)
    app.add_api_route("/late/private", served)
    return app


def starlette_app() -> Starlette:
    """Return a Starlette app whose public routes are the paths in `exclude=`."""
    app = Starlette(
        routes=[
            Route("/numbers/{number:int}", answer),
            Route("/files/{name:path}", answer),
            Host("api.example", Starlette(routes=[Route("/hosted", answer)])),
        ]
    )
    Grelmicro(
        uses=[
            ErrorResponses(),
            table_authentication(
                exclude=("/numbers/*", "/files/*", "/hosted", "/late/public")
            ),
        ]
    ).install(app)
    app.add_route("/late/public", answer)
    app.add_route("/late/private", answer)
    return app


@get("/numbers/{number:int}", opt=LitestarAnonymous())
async def number(number: FromPath[int]) -> int:
    """Answer on a Litestar route with an int parameter."""
    return number


@get("/files/{name:path}", opt=LitestarAnonymous())
async def file(name: FromPath[str]) -> str:
    """Answer on a Litestar route with a path parameter."""
    return name


@get("/late/public", opt=LitestarAnonymous())
async def late_public() -> str:
    """Answer on a public Litestar route registered after install."""
    return "public"


@get("/late/private")
async def late_private() -> str:
    """Answer on a protected Litestar route registered after install."""
    return "private"


def litestar_app() -> Litestar:
    """Return a Litestar app declaring each public handler with `Anonymous()`."""
    app = Litestar(route_handlers=[number, file], openapi_config=None)
    Grelmicro(uses=[ErrorResponses(), table_authentication()]).install(app)
    app.register(late_public)
    app.register(late_private)
    return app


FRAMEWORKS: dict[str, tuple[Callable[[], Any], type[Any]]] = {
    "fastapi": (fastapi_app, TestClient),
    "starlette": (starlette_app, TestClient),
    "litestar": (litestar_app, LitestarTestClient),
}


@pytest.mark.parametrize(
    ("framework", "case"),
    [
        *(
            pytest.param(framework, case, id=f"{framework}: {case.name}")
            for framework in FRAMEWORKS
            for case in ROUTED
        ),
        *(
            pytest.param(framework, case, id=f"{framework}: {case.name}")
            for framework in ("fastapi", "starlette")
            for case in HOSTED
        ),
    ],
)
def test_authenticated_requests_url_a_router_reads_its_own_way_answers_its_pinned_status(
    framework: str, case: Case
) -> None:
    """Without a credential and with a valid one, the status is the row's."""
    # Arrange
    build, client_class = FRAMEWORKS[framework]

    # Act
    statuses = answered(build, client_class, case)

    # Assert
    assert statuses == getattr(case, framework)
