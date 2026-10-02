"""The status every kind of request is answered with, on each framework.

`AuthenticatedRequests` verifies a credential before the framework routes
the request, and the gate of the route it reaches decides the rest,
without the routes read before routing. This table pins what that
decision answers for each kind of request, with no credential and with a
valid one, on FastAPI, Starlette and Litestar. A change to how the decision
is made must keep every row.

Each row reads `(without a credential, with a valid one)`. Where the
frameworks differ, they differ on their own terms:

- Plain Starlette has no route declaration, so its public routes are the
  paths named in `exclude=`, which match the path whatever the method and
  whichever route answers it.
- Litestar routes a trailing slash to the route without one instead of
  redirecting, and answers a CORS preflight `204`.
- Litestar runs the app's own middleware behind its router, so a response
  it writes comes after authentication.

A websocket handshake is recorded as `101` when the handler accepted it. A
test client without the denial response extension can only see a refused
handshake closed, which a server answers `403`, and that is recorded then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from litestar import Litestar, Router, asgi, get, websocket
from litestar import WebSocket as LitestarWebSocket
from litestar.config.cors import CORSConfig
from litestar.exceptions import (
    WebSocketDisconnect as LitestarWebSocketDisconnect,
)
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from grelmicro import Grelmicro
from grelmicro.errors import MiddlewarePlacementWarning
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.http._authentication import _PublicRoutes
from grelmicro.integrations.fastapi import Anonymous
from grelmicro.integrations.litestar import Anonymous as LitestarAnonymous
from tests.test_authentication import bearer, token, verifier

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(5)]

ORIGIN = "https://app.example"
ROOT_PATH = "/svc"
ACCEPTED = 101
"""A websocket handshake the handler accepted."""
CLOSED = 403
"""A websocket handshake closed before it completed."""
RESOURCE = "https://api.example"
ISSUER = "https://auth.example"
METADATA = "/.well-known/oauth-protected-resource"
"""Where the middleware serves the protected resource metadata of `RESOURCE`."""
PREFLIGHT = {"origin": ORIGIN, "access-control-request-method": "GET"}
MAINTENANCE = {"x-maintenance": "on"}


@dataclass(frozen=True)
class Case:
    """One request, and what each framework answers it with today."""

    name: str
    path: str
    fastapi: tuple[int, int]
    starlette: tuple[int, int]
    litestar: tuple[int, int]
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    root_path: str = ""
    websocket: bool = False


CASES = (
    Case(
        "public route",
        "/public",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "protected route",
        "/private",
        fastapi=(401, 200),
        starlette=(401, 200),
        litestar=(401, 200),
    ),
    Case(
        "no route",
        "/nowhere",
        fastapi=(401, 404),
        starlette=(401, 404),
        litestar=(401, 404),
    ),
    Case(
        "method the public route does not serve",
        "/public",
        method="DELETE",
        fastapi=(401, 405),
        starlette=(405, 405),
        litestar=(401, 405),
    ),
    Case(
        "method the protected route does not serve",
        "/private",
        method="DELETE",
        fastapi=(401, 405),
        starlette=(401, 405),
        litestar=(401, 405),
    ),
    Case(
        "HEAD to a protected route",
        "/private",
        method="HEAD",
        fastapi=(401, 405),
        starlette=(401, 200),
        litestar=(401, 405),
    ),
    Case(
        "matrix parameter on a protected route",
        "/private;x=1",
        fastapi=(401, 404),
        starlette=(401, 404),
        litestar=(401, 404),
    ),
    Case(
        "case changed on a protected route",
        "/PRIVATE",
        fastapi=(401, 404),
        starlette=(401, 404),
        litestar=(401, 404),
    ),
    Case(
        "leading double slash",
        "//private",
        fastapi=(401, 404),
        starlette=(401, 404),
        litestar=(401, 404),
    ),
    Case(
        "encoded slash after a protected route",
        "/private%2F",
        fastapi=(401, 307),
        starlette=(401, 307),
        litestar=(401, 200),
    ),
    Case(
        "carriage return after a protected route",
        "/private%0D",
        fastapi=(401, 404),
        starlette=(401, 404),
        litestar=(401, 404),
    ),
    Case(
        "line feed after a protected route",
        "/private%0A",
        fastapi=(401, 200),
        starlette=(401, 200),
        litestar=(401, 404),
    ),
    Case(
        "protected resource metadata",
        METADATA,
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "trailing slash to a public route",
        "/public/",
        fastapi=(401, 307),
        starlette=(401, 307),
        litestar=(200, 200),
    ),
    Case(
        "trailing slash to a protected route",
        "/private/",
        fastapi=(401, 307),
        starlette=(401, 307),
        litestar=(401, 200),
    ),
    Case(
        "mounted app, public route",
        "/sub/public",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "mounted app, protected route",
        "/sub/private",
        fastapi=(401, 200),
        starlette=(401, 200),
        litestar=(401, 200),
    ),
    Case(
        "mounted router, public route",
        "/api/public",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "mounted router, protected route",
        "/api/private",
        fastapi=(401, 200),
        starlette=(401, 200),
        litestar=(401, 200),
    ),
    Case(
        "root path, public route",
        "/svc/public",
        root_path=ROOT_PATH,
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
    Case(
        "root path, protected route",
        "/svc/private",
        root_path=ROOT_PATH,
        fastapi=(401, 200),
        starlette=(401, 200),
        litestar=(401, 200),
    ),
    Case(
        "CORS preflight to a protected route",
        "/private",
        method="OPTIONS",
        headers=PREFLIGHT,
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(204, 204),
    ),
    Case(
        "the app's own middleware answers",
        "/private",
        headers=MAINTENANCE,
        fastapi=(503, 503),
        starlette=(503, 503),
        litestar=(401, 503),
    ),
    Case(
        "websocket, public route",
        "/ws/public",
        websocket=True,
        fastapi=(ACCEPTED, ACCEPTED),
        starlette=(ACCEPTED, ACCEPTED),
        litestar=(ACCEPTED, ACCEPTED),
    ),
    Case(
        "websocket, protected route",
        "/ws/private",
        websocket=True,
        fastapi=(401, ACCEPTED),
        starlette=(401, ACCEPTED),
        litestar=(CLOSED, ACCEPTED),
    ),
    Case(
        "a public and a protected route could answer",
        "/items/featured",
        fastapi=(401, 200),
        starlette=(200, 200),
        litestar=(401, 200),
    ),
    Case(
        "only the public route answers",
        "/items/7",
        fastapi=(200, 200),
        starlette=(200, 200),
        litestar=(200, 200),
    ),
)


async def endpoint(request: Request) -> JSONResponse:
    """Answer with the path the request arrived at."""
    return JSONResponse({"path": request.url.path})


async def served() -> dict[str, bool]:
    """Answer on a FastAPI route."""
    return {"served": True}


async def accept(websocket: WebSocket) -> None:
    """Accept the handshake and close."""
    await websocket.accept()
    await websocket.close()


class Maintenance:
    """Answer `503` itself, before routing, when asked to."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Answer `503` to a request carrying `x-maintenance`, else pass it on."""
        if (
            scope["type"] == "http"
            and (b"x-maintenance", b"on") in scope["headers"]
        ):
            await PlainTextResponse("down", status_code=503)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


def authenticated(**options: Any) -> AuthenticatedRequests:  # noqa: ANN401
    """Return the authentication every app in the table installs."""
    return AuthenticatedRequests(
        verifier(),
        resource=RESOURCE,
        authorization_servers=[ISSUER],
        **options,
    )


def installed(app: Any) -> Any:  # noqa: ANN401
    """Install authentication on `app` and return it."""
    Grelmicro(uses=[ErrorResponses(), authenticated()]).install(app)
    return app


def fastapi_app() -> FastAPI:
    """Return a FastAPI app with a route of each kind the table asks."""
    app = FastAPI()
    app.add_middleware(Maintenance)
    app.add_middleware(
        CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"]
    )
    app.add_api_route("/public", served, dependencies=[Anonymous()])
    app.add_api_route("/private", served)
    app.add_api_route("/items/featured", served)
    app.add_api_route("/items/{item_id}", served, dependencies=[Anonymous()])
    app.add_api_websocket_route(
        "/ws/public", accept, dependencies=[Anonymous()]
    )
    app.add_api_websocket_route("/ws/private", accept)
    router = APIRouter()
    router.add_api_route("/public", served, dependencies=[Anonymous()])
    router.add_api_route("/private", served)
    app.include_router(router, prefix="/api")
    sub = FastAPI()
    sub.add_api_route("/public", served, dependencies=[Anonymous()])
    sub.add_api_route("/private", served)
    app.mount("/sub", sub)
    return installed(app)


def starlette_app() -> Starlette:
    """Return a Starlette app with a route of each kind the table asks."""
    app = Starlette(
        routes=[
            Route("/public", endpoint),
            Route("/private", endpoint),
            Route("/items/featured", endpoint),
            Route("/items/{item_id}", endpoint),
            WebSocketRoute("/ws/public", accept),
            WebSocketRoute("/ws/private", accept),
            Mount(
                "/api",
                routes=[
                    Route("/public", endpoint),
                    Route("/private", endpoint),
                ],
            ),
            Mount(
                "/sub",
                app=Starlette(
                    routes=[
                        Route("/public", endpoint),
                        Route("/private", endpoint),
                    ]
                ),
            ),
        ],
        middleware=[
            Middleware(
                CORSMiddleware, allow_origins=[ORIGIN], allow_methods=["*"]
            ),
            Middleware(Maintenance),
        ],
    )
    Grelmicro(
        uses=[
            ErrorResponses(),
            authenticated(
                exclude=(
                    "/public",
                    "/items/*",
                    "/ws/public",
                    "/api/public",
                    "/sub/public",
                ),
            ),
        ]
    ).install(app)
    return app


async def litestar_served() -> str:
    """Answer on a Litestar route."""
    return "served"


async def litestar_accept(socket: LitestarWebSocket[Any, Any, Any]) -> None:
    """Accept the handshake and close."""
    await socket.accept()
    await socket.close()


async def litestar_mounted(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer on a mounted ASGI app."""
    await PlainTextResponse("served")(scope, receive, send)


def declared(*, public: bool) -> dict[str, Any]:
    """Return the `opt` a Litestar handler declares itself with."""
    return {**LitestarAnonymous()} if public else {}


def litestar_app() -> Litestar:
    """Return a Litestar app with a handler of each kind the table asks."""
    app = Litestar(
        route_handlers=[
            get("/public", opt=declared(public=True))(litestar_served),
            get("/private", opt=declared(public=False))(litestar_served),
            get("/items/featured", opt=declared(public=False))(litestar_served),
            get("/items/{item_id:str}", opt=declared(public=True))(
                litestar_served
            ),
            websocket("/ws/public", opt=declared(public=True))(litestar_accept),
            websocket("/ws/private", opt=declared(public=False))(
                litestar_accept
            ),
            Router(
                "/api",
                route_handlers=[
                    get("/public", opt=declared(public=True))(litestar_served),
                    get("/private", opt=declared(public=False))(
                        litestar_served
                    ),
                ],
            ),
            asgi(
                "/sub/public",
                is_mount=True,
                copy_scope=False,
                opt=declared(public=True),
            )(litestar_mounted),
            asgi(
                "/sub/private",
                is_mount=True,
                copy_scope=False,
                opt=declared(public=False),
            )(litestar_mounted),
        ],
        middleware=[cast("Any", Maintenance)],
        cors_config=CORSConfig(allow_origins=[ORIGIN]),
    )
    # The app's own middleware runs behind the router, so authentication,
    # which wraps the whole app, runs before it.
    with pytest.warns(MiddlewarePlacementWarning):
        return installed(app)


def status(
    client: Any,  # noqa: ANN401
    case: Case,
    headers: dict[str, str],
) -> int:
    """Return the status `case` is answered with, sending `headers`."""
    headers = {**case.headers, **headers}
    if not case.websocket:
        return client.request(
            case.method, case.path, headers=headers, follow_redirects=False
        ).status_code
    try:
        with client.websocket_connect(case.path, headers=headers):
            return ACCEPTED
    except WebSocketDenialResponse as denied:
        return denied.status_code
    except WebSocketDisconnect, LitestarWebSocketDisconnect:
        return CLOSED


def answered(
    build: Callable[[], Any],
    client_class: type[Any],
    case: Case,
) -> tuple[int, int]:
    """Return the status `case` is answered with, without and with a token."""
    app = build()
    with client_class(app, root_path=case.root_path) as client:
        return (
            status(client, case, {}),
            status(client, case, bearer(token())),
        )


def unasked(self: _PublicRoutes, scope: Any) -> bool:  # noqa: ANN401, ARG001
    """Fail the test: the routes read before routing were asked."""
    msg = f"the router emulation was asked about {scope['path']}"
    raise AssertionError(msg)


FRAMEWORKS = {
    "fastapi": (fastapi_app, TestClient),
    "starlette": (starlette_app, TestClient),
    "litestar": (litestar_app, LitestarTestClient),
}


@pytest.mark.parametrize(
    ("framework", "case"),
    [
        pytest.param(framework, case, id=f"{framework}: {case.name}")
        for framework in FRAMEWORKS
        for case in CASES
    ],
)
def test_each_request_is_answered_as_today(
    framework: str, case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a credential and with a valid one, the status is pinned.

    Every framework decides at each route's gate, so the routes read before
    routing are never asked.
    """
    build, client_class = FRAMEWORKS[framework]
    monkeypatch.setattr(_PublicRoutes, "matches", unasked)

    assert answered(build, client_class, case) == getattr(case, framework)
