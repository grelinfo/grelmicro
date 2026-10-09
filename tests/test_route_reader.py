"""The route reader of an installed Starlette or Litestar app.

The request span, the access record and the security event of one request
name the same route, read by the outermost installed app.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, NamedTuple, cast

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from litestar import Litestar, Router, asgi, get, websocket
from litestar import WebSocket as LitestarWebSocket
from litestar.params import Parameter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.litestar import (
    Authenticated as LitestarAuthenticated,
)
from grelmicro.integrations.starlette import (
    Authenticated as StarletteAuthenticated,
)
from grelmicro.log import AccessLog
from grelmicro.trace import Trace, TraceExporterType
from tests.test_authentication import FORGER, bearer, token, verifier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

ACCESS = "grelmicro.access"
SECURITY = "grelmicro.security.events"
HTTP_200 = 200
HTTP_307 = 307
HTTP_401 = 401
HTTP_403 = 403
HTTP_404 = 404
HTTP_405 = 405


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """Return the access records and the security events written meanwhile."""
    kept: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            kept.append(record)

    handler = Keep()
    loggers = [logging.getLogger(name) for name in (ACCESS, SECURITY)]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield kept
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)


class Reading(NamedTuple):
    """What one request recorded: the status and the route each record names."""

    status: int
    span: str | None
    access: list[str | None]
    security: list[str | None]


def _read(
    app: Any,  # noqa: ANN401
    method: str,
    path: str,
    records: list[logging.LogRecord],
    *,
    credential: str = "valid",
    root_path: str = "",
) -> Reading:
    """Install `app` with every consumer, send one request, return what it recorded.

    The request carries a `valid` token, one a `forged` signer signed, or
    one `unscoped` that a gated route refuses, since no token holds a scope.
    """
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            Trace(exporter=TraceExporterType.NONE),
            AccessLog(),
            AuthenticatedRequests(verifier()),
        ]
    )
    micro.install(app)
    exporter = InMemorySpanExporter()
    with TestClient(
        app, raise_server_exceptions=False, root_path=root_path
    ) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        response = client.request(
            method,
            path,
            headers=bearer(
                token(FORGER) if credential == "forged" else token()
            ),
            follow_redirects=False,
        )
    [server] = [
        span
        for span in exporter.get_finished_spans()
        if span.kind is SpanKind.SERVER
    ]
    attributes = server.attributes or {}
    span_route = attributes.get("http.route")
    return Reading(
        response.status_code,
        None if span_route is None else str(span_route),
        [
            record.__dict__.get("http.route")
            for record in records
            if record.name == ACCESS
        ],
        [
            record.__dict__.get("http.route")
            for record in records
            if record.name == SECURITY
        ],
    )


async def _item(request: Request) -> JSONResponse:
    return JSONResponse({"n": int(request.path_params["n"])})


async def _ok(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _file(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("file")(scope, receive, send)


def _items() -> list[Route]:
    return [Route("/items/{n:int}", _item)]


@StarletteAuthenticated(scopes=["items:write"])
async def _gated(request: Request) -> JSONResponse:
    return JSONResponse(
        {"n": int(request.path_params["n"])}
    )  # pragma: no cover


def _gated_items() -> list[Route]:
    return [Route("/items/{n:int}", _gated)]


def _litestar_items() -> Litestar:
    """Return a Litestar app serving `GET /items/{n}`."""

    @get("/items/{n:int}")
    async def item(n: Annotated[int, Parameter()]) -> int:
        return n

    return Litestar(route_handlers=[item], logging_config=None)


def _fastapi_items() -> FastAPI:
    """Return a FastAPI app serving `GET /items/{n}`."""
    app = FastAPI()

    @app.get("/items/{n}")
    async def item(n: int) -> int:
        return n

    return app


def _fastapi_router() -> FastAPI:
    """Return a FastAPI app serving `GET /v1/items/{n}` from an included router."""
    router = APIRouter(prefix="/v1")

    @router.get("/items/{n}")
    async def item(n: int) -> int:
        return n

    app = FastAPI()
    app.include_router(router)
    return app


def _fastapi_plain() -> FastAPI:
    """Return a FastAPI app holding the Starlette route `/plain/{n}`."""
    app = FastAPI()
    app.add_route("/plain/{n:int}", _item)
    return app


def _wrapped_docs() -> Any:  # noqa: ANN401
    """Return a Starlette app serving `/docs/`, wrapped in a middleware."""
    inner = Starlette(routes=[Route("/docs/", _ok)])
    return CORSMiddleware(inner, allow_origins=["https://app.example"])


STARLETTE_SHAPES: dict[str, Callable[[], Starlette]] = {
    "route": lambda: Starlette(routes=_items()),
    "mount": lambda: Starlette(routes=[Mount("/v1", routes=_items())]),
    "parameterised-mount": lambda: Starlette(
        routes=[Mount("/t/{tenant}", routes=_items())]
    ),
    "nested-mount": lambda: Starlette(
        routes=[Mount("/t/{tenant}", routes=[Mount("/v/{v}", routes=_items())])]
    ),
    "asgi-mount": lambda: Starlette(
        routes=[Mount("/files/{bucket}", app=_file)]
    ),
    "wrapped-app": lambda: Starlette(
        routes=[Mount("/t/{tenant}", app=_wrapped_docs())]
    ),
    "slash-redirect": lambda: Starlette(
        routes=[Mount("/t/{tenant}", routes=[Route("/docs/", _ok)])]
    ),
    "litestar-app": lambda: Starlette(
        routes=[Mount("/shop", app=cast("Any", _litestar_items()))]
    ),
    "litestar-app-at-the-root": lambda: Starlette(
        routes=[Mount("", app=cast("Any", _litestar_items()))]
    ),
    "litestar-asgi-mount": lambda: Starlette(
        routes=[
            Mount("/s", app=cast("Any", _litestar_routes(_litestar_files())))
        ]
    ),
    "fastapi-app": lambda: Starlette(
        routes=[Mount("/api", app=_fastapi_items())]
    ),
    "fastapi-router": lambda: Starlette(
        routes=[Mount("/api", app=_fastapi_router())]
    ),
    "fastapi-plain-route": lambda: Starlette(
        routes=[Mount("/api", app=_fastapi_plain())]
    ),
    "gated-route": lambda: Starlette(routes=_gated_items()),
    "gated-mount": lambda: Starlette(
        routes=[Mount("/t/{tenant}", routes=_gated_items())]
    ),
}


@pytest.mark.parametrize(
    ("shape", "method", "path", "status", "route"),
    [
        ("route", "GET", "/items/3", HTTP_200, "/items/{n}"),
        ("route", "GET", "/nowhere", HTTP_404, None),
        ("mount", "GET", "/v1/items/3", HTTP_200, "/v1/items/{n}"),
        (
            "parameterised-mount",
            "GET",
            "/t/acme/items/3",
            HTTP_200,
            "/t/{tenant}/items/{n}",
        ),
        (
            "parameterised-mount",
            "POST",
            "/t/acme/items/3",
            HTTP_405,
            "/t/{tenant}/items/{n}",
        ),
        (
            "parameterised-mount",
            "GET",
            "/t/acme/nowhere",
            HTTP_404,
            "/t/{tenant}/{path}",
        ),
        (
            "nested-mount",
            "GET",
            "/t/acme/v/2/items/3",
            HTTP_200,
            "/t/{tenant}/v/{v}/items/{n}",
        ),
        (
            "asgi-mount",
            "GET",
            "/files/b1/a/b.txt",
            HTTP_200,
            "/files/{bucket}/{path}",
        ),
        ("wrapped-app", "GET", "/t/acme/docs", HTTP_307, "/t/{tenant}/docs/"),
        (
            "slash-redirect",
            "GET",
            "/t/acme/docs",
            HTTP_307,
            "/t/{tenant}/docs/",
        ),
        ("litestar-app", "GET", "/shop/items/3", HTTP_200, "/shop/items/{n}"),
        (
            "litestar-app-at-the-root",
            "GET",
            "/items/3",
            HTTP_200,
            "/items/{n}",
        ),
        (
            "litestar-asgi-mount",
            "GET",
            "/s/files/a.txt",
            HTTP_200,
            "/s/files/{path}",
        ),
        ("fastapi-app", "GET", "/api/items/3", HTTP_200, "/api/items/{n}"),
        (
            "fastapi-router",
            "GET",
            "/api/v1/items/3",
            HTTP_200,
            "/api/v1/items/{n}",
        ),
        (
            "fastapi-plain-route",
            "GET",
            "/api/plain/3",
            HTTP_200,
            "/api/plain/{n}",
        ),
    ],
)
def test_starlette_names_one_route_per_request(
    records: list[logging.LogRecord],
    *,
    shape: str,
    method: str,
    path: str,
    status: int,
    route: str | None,
) -> None:
    """The request span and the access record name the route the reader reads."""
    reading = _read(STARLETTE_SHAPES[shape](), method, path, records)

    assert reading.status == status
    assert reading.span == route
    assert reading.access == [route]
    assert reading.security == []


REFUSAL = ("shape", "method", "path", "root_path", "credential", "status")
"""What a refusal case sends, and the status it is refused with."""


@pytest.mark.parametrize(
    (*REFUSAL, "route", "span"),
    [
        (
            "route",
            "GET",
            "/items/3",
            "",
            "forged",
            HTTP_401,
            "/items/{n}",
            None,
        ),
        (
            "route",
            "GET",
            "/items/3",
            "/proxy",
            "forged",
            HTTP_401,
            "/proxy/items/{n}",
            None,
        ),
        (
            "route",
            "POST",
            "/items/3",
            "",
            "forged",
            HTTP_401,
            "/items/{n}",
            None,
        ),
        (
            "mount",
            "GET",
            "/v1/items/3",
            "",
            "forged",
            HTTP_401,
            "/v1/items/{n}",
            "/v1/items/{n}",
        ),
        (
            "parameterised-mount",
            "GET",
            "/t/acme/items/3",
            "",
            "forged",
            HTTP_401,
            "/t/{tenant}/items/{n}",
            "/t/{tenant}/items/{n}",
        ),
        (
            "parameterised-mount",
            "POST",
            "/t/acme/items/3",
            "",
            "forged",
            HTTP_401,
            "/t/{tenant}/items/{n}",
            "/t/{tenant}/items/{n}",
        ),
        (
            "asgi-mount",
            "GET",
            "/files/b1/a/b.txt",
            "",
            "forged",
            HTTP_401,
            "/files/{bucket}/{path}",
            "/files/{bucket}/{path}",
        ),
        (
            "slash-redirect",
            "GET",
            "/t/acme/docs",
            "",
            "forged",
            HTTP_401,
            "/t/{tenant}/{path}",
            "/t/{tenant}/{path}",
        ),
        (
            "litestar-app",
            "GET",
            "/shop/items/3",
            "",
            "forged",
            HTTP_401,
            "/shop/{path}",
            "/shop/{path}",
        ),
        (
            "fastapi-app",
            "GET",
            "/api/items/3",
            "",
            "forged",
            HTTP_401,
            "/api/items/{n}",
            "/api/items/{n}",
        ),
        (
            "gated-route",
            "GET",
            "/items/3",
            "",
            "unscoped",
            HTTP_403,
            "/items/{n}",
            "/items/{n}",
        ),
        (
            "gated-route",
            "GET",
            "/items/3",
            "/proxy",
            "unscoped",
            HTTP_403,
            "/proxy/items/{n}",
            "/proxy/items/{n}",
        ),
        (
            "gated-mount",
            "GET",
            "/t/acme/items/3",
            "",
            "unscoped",
            HTTP_403,
            "/t/{tenant}/items/{n}",
            "/t/{tenant}/items/{n}",
        ),
    ],
)
def test_starlette_names_one_route_for_a_refused_request(
    records: list[logging.LogRecord],
    *,
    shape: str,
    method: str,
    path: str,
    root_path: str,
    credential: str,
    status: int,
    route: str,
    span: str | None,
) -> None:
    """A refused request names one route in its access record and its security event.

    The request span names it too, except for a request refused before
    the router ran outside any mount, which names none.
    """
    reading = _read(
        STARLETTE_SHAPES[shape](),
        method,
        path,
        records,
        credential=credential,
        root_path=root_path,
    )

    assert reading.status == status
    assert reading.span == span
    assert reading.access == [route]
    assert reading.security == [route]


def _slashed_starlette() -> Starlette:
    """Return a Starlette app serving `/items/{n}/`, the path a Litestar mount passes on."""
    return Starlette(routes=[Route("/items/{n:int}/", _item)])


def _litestar_mounting(inner: object, at: str = "/shop") -> Litestar:
    """Return a Litestar app mounting `inner` at `at`, sharing its scope."""

    @asgi(at, is_mount=True, copy_scope=False)
    async def shop(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await inner(scope, receive, send)  # type: ignore[operator]  # ty: ignore[call-non-callable]

    return Litestar(route_handlers=[shop], logging_config=None)


def _litestar_routes(*handlers: Any) -> Litestar:  # noqa: ANN401
    return Litestar(route_handlers=list(handlers), logging_config=None)


def _litestar_item() -> Any:  # noqa: ANN401
    @get("/items/{n:int}")
    async def item(n: Annotated[int, Parameter()]) -> int:
        return n

    return item


def _litestar_gated() -> Any:  # noqa: ANN401
    @get(
        "/items/{n:int}", guards=[LitestarAuthenticated(scopes=["items:write"])]
    )
    async def item(n: Annotated[int, Parameter()]) -> int:
        return n  # pragma: no cover

    return item


def _litestar_files() -> Any:  # noqa: ANN401
    @asgi("/files", is_mount=True, copy_scope=True)
    async def files(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await _file(scope, receive, send)

    return files


LITESTAR_SHAPES: dict[str, Callable[[], Litestar]] = {
    "route": lambda: _litestar_routes(_litestar_item()),
    "router": lambda: _litestar_routes(
        Router("/v1", route_handlers=[_litestar_item()])
    ),
    "parameterised-router": lambda: _litestar_routes(
        Router("/t/{tenant:str}", route_handlers=[_litestar_item()])
    ),
    "asgi-mount": lambda: _litestar_routes(_litestar_files()),
    "litestar-app": lambda: _litestar_mounting(_litestar_items()),
    "starlette-app": lambda: _litestar_mounting(_slashed_starlette()),
    "gated-route": lambda: _litestar_routes(_litestar_gated()),
    "gated-router": lambda: _litestar_routes(
        Router("/t/{tenant:str}", route_handlers=[_litestar_gated()])
    ),
}


@pytest.mark.parametrize(
    ("shape", "method", "path", "status", "route"),
    [
        ("route", "GET", "/items/3", HTTP_200, "/items/{n}"),
        ("route", "POST", "/items/3", HTTP_405, "/items/{n}"),
        ("route", "GET", "/items/3/", HTTP_200, "/items/{n}"),
        ("route", "GET", "/nowhere", HTTP_404, None),
        ("router", "GET", "/v1/items/3", HTTP_200, "/v1/items/{n}"),
        (
            "parameterised-router",
            "GET",
            "/t/acme/items/3",
            HTTP_200,
            "/t/{tenant}/items/{n}",
        ),
        ("asgi-mount", "GET", "/files/a/b", HTTP_200, "/files/{path}"),
        ("litestar-app", "GET", "/shop/items/3", HTTP_200, "/shop/items/{n}"),
        ("litestar-app", "GET", "/shop/nowhere", HTTP_404, "/shop/{path}"),
        ("starlette-app", "GET", "/shop/items/3", HTTP_200, "/shop/{path}"),
    ],
)
def test_litestar_names_one_route_per_request(
    records: list[logging.LogRecord],
    *,
    shape: str,
    method: str,
    path: str,
    status: int,
    route: str | None,
) -> None:
    """The request span and the access record name the route the reader reads.

    Litestar takes a trailing slash off rather than redirecting.
    """
    reading = _read(LITESTAR_SHAPES[shape](), method, path, records)

    assert reading.status == status
    assert reading.span == route
    assert reading.access == [route]
    assert reading.security == []


@pytest.mark.parametrize(
    (*REFUSAL, "route", "span"),
    [
        (
            "route",
            "GET",
            "/items/3",
            "",
            "forged",
            HTTP_401,
            "/items/{n}",
            None,
        ),
        (
            "route",
            "GET",
            "/items/3",
            "/proxy",
            "forged",
            HTTP_401,
            "/proxy/items/{n}",
            None,
        ),
        (
            "route",
            "POST",
            "/items/3",
            "",
            "forged",
            HTTP_401,
            None,
            None,
        ),
        (
            "parameterised-router",
            "GET",
            "/t/acme/items/3",
            "",
            "forged",
            HTTP_401,
            "/t/{tenant}/items/{n}",
            None,
        ),
        (
            "asgi-mount",
            "GET",
            "/files/a/b",
            "",
            "forged",
            HTTP_401,
            "/files/{path}",
            "/files/{path}",
        ),
        (
            "litestar-app",
            "GET",
            "/shop/items/3",
            "",
            "forged",
            HTTP_401,
            "/shop/{path}",
            "/shop/{path}",
        ),
        (
            "starlette-app",
            "GET",
            "/shop/items/3",
            "",
            "forged",
            HTTP_401,
            "/shop/{path}",
            "/shop/{path}",
        ),
        (
            "gated-route",
            "GET",
            "/items/3",
            "",
            "unscoped",
            HTTP_403,
            "/items/{n}",
            "/items/{n}",
        ),
        (
            "gated-route",
            "GET",
            "/items/3",
            "/proxy",
            "unscoped",
            HTTP_403,
            "/proxy/items/{n}",
            "/proxy/items/{n}",
        ),
        (
            "gated-router",
            "GET",
            "/t/acme/items/3",
            "",
            "unscoped",
            HTTP_403,
            "/t/{tenant}/items/{n}",
            "/t/{tenant}/items/{n}",
        ),
    ],
)
def test_litestar_names_one_route_for_a_refused_request(
    records: list[logging.LogRecord],
    *,
    shape: str,
    method: str,
    path: str,
    root_path: str,
    credential: str,
    status: int,
    route: str,
    span: str | None,
) -> None:
    """A refused request names one route in its access record and its security event.

    The request span names it too, except for a request refused before
    the router ran outside any mount, which names none.
    """
    reading = _read(
        LITESTAR_SHAPES[shape](),
        method,
        path,
        records,
        credential=credential,
        root_path=root_path,
    )

    assert reading.status == status
    assert reading.span == span
    assert reading.access == [route]
    assert reading.security == [route]


@pytest.mark.parametrize(
    "build", [STARLETTE_SHAPES["route"], LITESTAR_SHAPES["route"]]
)
def test_a_prefix_a_proxy_stripped_stays_on_the_route(
    records: list[logging.LogRecord], build: Callable[[], Any]
) -> None:
    """The root path goes on the route, whether the path carries it or not."""
    reading = _read(build(), "GET", "/items/3", records, root_path="/proxy")

    assert reading.status == HTTP_200
    assert reading.span == "/proxy/items/{n}"
    assert reading.access == ["/proxy/items/{n}"]


def _starlette_mounting(inner: object) -> Starlette:
    return Starlette(routes=[Mount("/shop", app=cast("Any", inner))])


@pytest.mark.parametrize(
    ("outer", "inner", "route"),
    [
        (_litestar_mounting, _litestar_items, "/shop/items/{n}"),
        (_litestar_mounting, _slashed_starlette, "/shop/{path}"),
        (
            _starlette_mounting,
            lambda: Starlette(routes=_items()),
            "/shop/items/{n}",
        ),
        (_starlette_mounting, _litestar_items, "/shop/items/{n}"),
    ],
    ids=[
        "litestar-in-litestar",
        "starlette-in-litestar",
        "starlette-in-starlette",
        "litestar-in-starlette",
    ],
)
async def test_a_request_through_two_installed_apps_writes_one_access_record(
    records: list[logging.LogRecord],
    *,
    outer: Callable[[object], Any],
    inner: Callable[[], Any],
    route: str,
) -> None:
    """The outermost installed app writes it, naming the route it reads."""
    mounted = inner()
    inner_micro = Grelmicro(uses=[AccessLog()])
    inner_micro.install(mounted)
    app = outer(mounted)
    outer_micro = Grelmicro(uses=[AccessLog()])
    outer_micro.install(app)

    async with (
        inner_micro,
        outer_micro,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        response = await client.get("/shop/items/3")

    [access] = [record for record in records if record.name == ACCESS]
    assert response.status_code == HTTP_200
    assert access.__dict__.get("http.route") == route


async def _echo(socket: WebSocket) -> None:
    await socket.accept()
    await socket.send_text(await socket.receive_text())
    await socket.close()


@websocket("/ws")
async def _litestar_echo(socket: LitestarWebSocket[Any, Any, Any]) -> None:
    await socket.accept()
    await socket.send_text(await socket.receive_text())
    await socket.close()


@pytest.mark.parametrize(
    "build",
    [
        lambda: Starlette(routes=[WebSocketRoute("/ws", _echo)]),
        lambda: _litestar_routes(_litestar_echo),
    ],
    ids=["starlette", "litestar"],
)
def test_a_websocket_is_served_and_writes_no_access_record(
    records: list[logging.LogRecord], build: Callable[[], Any]
) -> None:
    """The reader is left on a WebSocket too, and the access log leaves it alone."""
    app = build()
    micro = Grelmicro(
        uses=[Trace(exporter=TraceExporterType.NONE), AccessLog()]
    )
    micro.install(app)

    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        socket.send_text("hello")
        echoed = socket.receive_text()

    assert echoed == "hello"
    assert [record for record in records if record.name == ACCESS] == []
