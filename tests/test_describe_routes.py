"""`micro.describe(app)` and `grelmicro check` read the routes the integration declares.

One row per method a route declares, and a pattern matched against each
route's template one path segment per parameter, on FastAPI, Starlette
and Litestar alike.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Literal

import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from litestar import Controller, Litestar, asgi, get, post, websocket
from litestar import Router as LitestarRouter
from litestar import WebSocket as LitestarWebSocket
from litestar.params import FromPath
from starlette.applications import Starlette
from starlette.endpoints import HTTPEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Host, Mount, Route, Router, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.http import (
    AuthenticatedRequests,
    ErrorResponses,
    RouteDeclaration,
)
from grelmicro.integrations import fastapi as on_fastapi
from grelmicro.integrations import litestar as on_litestar
from grelmicro.integrations import starlette as on_starlette
from grelmicro.log import AccessLog
from grelmicro.testing import FakeVerifier, fake_claims
from tests.test_authentication_routing import catalog, publish

if TYPE_CHECKING:
    from pathlib import Path

    from grelmicro.describe import AppReport

Framework = Literal["fastapi", "litestar", "starlette"]
"""A framework an integration ships for."""

FRAMEWORKS: tuple[Framework, ...] = ("fastapi", "litestar", "starlette")
"""Every framework the reports are pinned on."""

OK = 200
"""Status a served request is answered with."""


async def _ok(_request: object = None) -> JSONResponse:
    """Answer any route."""
    return JSONResponse({})


async def _socket(_websocket: object) -> None:
    """Accept nothing, for a websocket route."""


async def _raw(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    """Serve a mounted ASGI app no router reads."""
    await PlainTextResponse("raw")(scope, receive, send)


class _ReadAndWrite(HTTPEndpoint):
    """An endpoint class answering `GET` and `POST`, with no `methods=`."""

    async def get(self, _request: object) -> JSONResponse:
        """Answer a read."""
        return JSONResponse({})

    async def post(self, _request: object) -> JSONResponse:
        """Answer a write."""
        return JSONResponse({})


class _OwnRouter(Router):
    """A router of a class of its own, which the integration gates as one route."""


def _starlette(static: Path) -> Starlette:
    """Return a Starlette app with every kind of route."""
    return Starlette(
        routes=[
            Route("/items/{item_id:int}", _ok, methods=["GET", "POST"]),
            Route("/files/{p:path}", _ok),
            Route("/ep", _ReadAndWrite),
            WebSocketRoute("/ws", _socket),
            Mount("/static", app=StaticFiles(directory=static)),
            Mount("/raw", app=_raw),
            Mount("/api", routes=[Route("/users/{uid}", _ok)]),
            Mount(
                "/wrapped",
                app=CORSMiddleware(
                    Starlette(routes=[Route("/x", _ok)]), allow_origins=["*"]
                ),
            ),
            Mount("/own", app=_OwnRouter(routes=[Route("/a", _ok)])),
            Host("api.example.com", app=Router(routes=[Route("/hosted", _ok)])),
        ]
    )


def _fastapi(static: Path) -> FastAPI:
    """Return a FastAPI app with every kind of route."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/items/{item_id:int}", methods=["GET", "POST"])
    async def item(item_id: int) -> dict[str, int]:
        return {"item": item_id}

    @app.get("/files/{p:path}")
    async def files(p: str) -> dict[str, str]:
        return {"file": p}

    @app.websocket("/ws")
    async def sock(websocket: WebSocket) -> None:
        await websocket.close()

    users = APIRouter()

    @users.get("/users/{uid}")
    async def user(uid: str) -> dict[str, str]:
        return {"user": uid}

    deep = APIRouter()

    @deep.post("/deep")
    async def deeper() -> dict[str, bool]:
        return {"deep": True}

    users.include_router(deep, prefix="/n")
    app.include_router(users, prefix="/api")
    app.routes.append(Route("/ep", _ReadAndWrite))
    app.mount("/static", StaticFiles(directory=static))
    app.mount("/raw", _raw)
    app.mount(
        "/wrapped",
        CORSMiddleware(
            Starlette(routes=[Route("/x", _ok)]), allow_origins=["*"]
        ),
    )
    app.host("api.example.com", Router(routes=[Route("/hosted", _ok)]))
    return app


@get("/items/{item_id:int}")
async def _item(item_id: FromPath[int]) -> dict[str, int]:
    """Read one item."""
    return {"item": item_id}


@post("/items/{item_id:int}")
async def _write_item(item_id: FromPath[int]) -> dict[str, int]:
    """Write one item."""
    return {"item": item_id}


@get("/files/{p:path}")
async def _files(p: FromPath[str]) -> dict[str, str]:
    """Read one file."""
    return {"file": p}


@websocket("/ws")
async def _litestar_socket(socket: LitestarWebSocket[Any, Any, Any]) -> None:
    """Close at once."""
    await socket.close()


@asgi("/raw", is_mount=True, copy_scope=True)
async def _litestar_raw(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    """Serve a mounted ASGI app."""
    await _raw(scope, receive, send)


@get("/users/{uid:str}")
async def _user(uid: FromPath[str]) -> dict[str, str]:
    """Read one user."""
    return {"user": uid}


class _Things(Controller):
    """A controller under its own path."""

    path = "/things"

    @get("/one")
    async def one(self) -> dict[str, bool]:
        """Read one thing."""
        return {"one": True}


@get("/late")
async def _late() -> dict[str, bool]:
    """Answer a handler registered after install."""
    return {"late": True}


def _litestar(_static: Path) -> Litestar:
    """Return a Litestar app with every kind of handler."""
    return Litestar(
        route_handlers=[
            _item,
            _write_item,
            _files,
            _litestar_socket,
            _litestar_raw,
            LitestarRouter("/api", route_handlers=[_user]),
            _Things,
        ],
        openapi_config=None,
    )


def _add_late(app: Any) -> None:  # noqa: ANN401
    """Add a `GET /late` route after install, the way each framework does."""
    if isinstance(app, Litestar):
        app.register(_late)
    elif isinstance(app, FastAPI):
        app.get("/late")(_ok)
    else:
        app.add_route("/late", _ok)


_BUILDERS = {
    "fastapi": _fastapi,
    "litestar": _litestar,
    "starlette": _starlette,
}
"""How each framework's app is built."""


def _described(framework: Framework, static: Path, *patterns: str) -> AppReport:
    """Return the report on the framework's app, a route added after install."""
    app = _BUILDERS[framework](static)
    micro = Grelmicro(uses=[AccessLog(include=patterns or ("/*",))])
    micro.install(app)
    _add_late(app)
    return micro.describe(app)


_STARLETTE_ROWS = (
    ("GET", "/api/users/{uid}"),
    ("GET", "/ep"),
    ("POST", "/ep"),
    ("GET", "/files/{p:path}"),
    ("GET", "/hosted"),
    ("GET", "/items/{item_id:int}"),
    ("POST", "/items/{item_id:int}"),
    ("GET", "/late"),
    ("GET", "/wrapped/x"),
)
"""The rows a Starlette app lists, each method its route declares."""

_FASTAPI_ROWS = (
    ("POST", "/api/n/deep"),
    ("GET", "/api/users/{uid}"),
    ("GET", "/ep"),
    ("POST", "/ep"),
    ("GET", "/files/{p:path}"),
    ("GET", "/hosted"),
    ("GET", "/items/{item_id:int}"),
    ("POST", "/items/{item_id:int}"),
    ("GET", "/late"),
    ("GET", "/wrapped/x"),
)
"""The rows a FastAPI app lists, included routers with every prefix."""

_LITESTAR_ROWS = (
    ("GET", "/api/users/{uid}"),
    ("OPTIONS", "/api/users/{uid}"),
    ("GET", "/files/{p:path}"),
    ("OPTIONS", "/files/{p:path}"),
    ("GET", "/items/{item_id}"),
    ("OPTIONS", "/items/{item_id}"),
    ("POST", "/items/{item_id}"),
    ("GET", "/late"),
    ("OPTIONS", "/late"),
    ("GET", "/things/one"),
    ("OPTIONS", "/things/one"),
)
"""The rows a Litestar app lists, each path as its router names it."""


@pytest.mark.parametrize(
    ("framework", "rows"),
    [
        pytest.param("fastapi", _FASTAPI_ROWS, id="fastapi"),
        pytest.param("litestar", _LITESTAR_ROWS, id="litestar"),
        pytest.param("starlette", _STARLETTE_ROWS, id="starlette"),
    ],
)
def test_describe_endpoints_list_every_declared_route_once_per_method(
    framework: Framework,
    rows: tuple[tuple[str, str], ...],
    tmp_path: Path,
) -> None:
    """Routers, hosts, wrapped mounts and a route added after install have rows.

    A mount of an app no router reads, a router of a class of its own and a
    websocket route are declared for every method, so they have none.
    """
    # Act
    report = _described(framework, tmp_path)

    # Assert
    assert tuple((row.method, row.path) for row in report.endpoints) == rows


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_check_include_url_names_a_route_one_path_segment_per_parameter(
    framework: Framework, tmp_path: Path
) -> None:
    """A parameter takes one segment whatever its converter, and a `path` parameter the rest."""
    # Act
    report = _described(
        framework, tmp_path, "/items/abc", "/files/a/b", "/items/1/x", "/nope"
    )

    # Assert
    warnings = [c.detail for c in report.checks if c.name == "path-patterns"]
    assert warnings == [
        "access_log/default names /items/1/x, /nope, which no route matches"
    ]


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_check_include_prefix_names_a_mounted_app(
    framework: Framework, tmp_path: Path
) -> None:
    """A prefix under a mounted ASGI app names the mount, which answers it."""
    # Act
    report = _described(framework, tmp_path, "/raw/*")

    # Assert
    assert not [c for c in report.checks if c.name == "path-patterns"]


_INTEGRATIONS = {
    "fastapi": on_fastapi,
    "litestar": on_litestar,
    "starlette": on_starlette,
}
"""The integration each framework's app loads."""


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_describe_reads_the_routes_only_from_the_integration(
    framework: Framework,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rows and the pattern check are the declarations listed, never the app's own routes."""
    # Arrange
    reads: list[object] = []
    listed = [
        RouteDeclaration("/listed/{key}", methods=frozenset({"GET", "HEAD"})),
        RouteDeclaration("/socket"),
    ]

    def declarations(app: object) -> list[RouteDeclaration]:
        reads.append(app)
        return listed

    app = _BUILDERS[framework](tmp_path)
    micro = Grelmicro(
        uses=[AccessLog(include=("/listed/7", "/socket", "/items/*"))]
    )
    micro.install(app)
    monkeypatch.setattr(
        _INTEGRATIONS[framework], "route_declarations", declarations
    )

    # Act
    report = micro.describe(app)

    # Assert
    assert {id(read) for read in reads} == {id(app)}
    assert [(row.method, row.path) for row in report.endpoints] == [
        ("GET", "/listed/{key}"),
    ]
    assert [c.detail for c in report.checks if c.name == "path-patterns"] == [
        "access_log/default names /items/*, which no route matches"
    ]


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_check_include_names_the_protected_resource_metadata(
    framework: Framework, tmp_path: Path
) -> None:
    """The metadata `AuthenticatedRequests` publishes is a path the app serves."""
    # Arrange
    app = _BUILDERS[framework](tmp_path)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(
                FakeVerifier(alice=fake_claims("alice")),
                resource="https://api.example.com/",
                authorization_servers=("https://issuer.example.com",),
            ),
            AccessLog(include=("/.well-known/*",)),
        ]
    )
    micro.install(app)

    # Act
    report = micro.describe(app)

    # Assert
    assert not [c for c in report.checks if c.name == "path-patterns"]


@pytest.mark.parametrize(
    ("integration", "warned"),
    [
        pytest.param("hookless", True, id="no-route-declarations"),
        pytest.param("listing", False, id="route-declarations"),
        pytest.param("offline", False, id="no-http"),
    ],
)
def test_check_integration_without_route_declarations_warns_routes_not_declared(
    integration: str,
    *,
    warned: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP integration listing no route says why the table is empty."""
    # Arrange
    publish(monkeypatch, integration)
    app = catalog()
    micro = Grelmicro(uses=[AccessLog(include=("/catalog",))])
    micro.install(app)

    # Act
    report = micro.describe(app)

    # Assert
    found = [c for c in report.checks if c.name == "routes-not-declared"]
    expected = (
        "the tinyweb integration has no route_declarations, so no endpoint "
        "is listed and no include= pattern is checked. Ship "
        "route_declarations in the integration."
    )
    assert [(c.status, c.detail) for c in found] == (
        [("warn", expected)] if warned else []
    )


def test_describe_lists_the_routes_of_a_mounted_bound_router() -> None:
    """A mount of a router's bound `app` is read as that router."""
    # Arrange
    router = Router(routes=[Route("/r/{u:uuid}", _ok)])
    app = Starlette(routes=[Mount("/b", app=router.app)])
    micro = Grelmicro(uses=[AccessLog()])
    micro.install(app)

    # Act
    report = micro.describe(app)
    with TestClient(app) as client:
        served = client.get(f"/b/r/{uuid.UUID(int=7)}")

    # Assert
    assert [(row.method, row.path) for row in report.endpoints] == [
        ("GET", "/b/r/{u:uuid}")
    ]
    assert served.status_code == OK


@pytest.mark.parametrize(
    ("framework", "url"),
    [
        pytest.param("fastapi", "/raw/a", id="fastapi-asgi-mount"),
        pytest.param("fastapi", "/static/a.css", id="fastapi-static"),
        pytest.param("litestar", "/raw/a", id="litestar-asgi-mount"),
        pytest.param("starlette", "/raw/a", id="starlette-asgi-mount"),
        pytest.param("starlette", "/static/a.css", id="starlette-static"),
        pytest.param("starlette", "/own/a", id="starlette-own-router"),
    ],
)
def test_check_include_url_a_mount_serves_never_warns(
    framework: Framework, url: str, tmp_path: Path
) -> None:
    """A path under a route declared for every method is one it serves."""
    # Act
    report = _described(framework, tmp_path, url)

    # Assert
    assert not [c for c in report.checks if c.name == "path-patterns"]
