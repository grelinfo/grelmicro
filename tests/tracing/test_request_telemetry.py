"""Starlette and Litestar request telemetry, as `micro.install(app)` wires it.

grelmicro records the request span and the HTTP server metrics itself, with
the names and attributes FastAPI records.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final

import anyio
import pytest
from fastapi import APIRouter, FastAPI
from litestar import Litestar, Router, asgi, get, websocket
from litestar import Request as LitestarRequest
from litestar import Response as LitestarResponseType
from litestar import WebSocket as LitestarWebSocket
from litestar.config.cors import CORSConfig
from litestar.exceptions import HTTPException as LitestarHTTPException
from litestar.params import Parameter
from litestar.response import Stream
from litestar.testing import TestClient as LitestarTestClient
from litestar.types import Receive as LitestarReceive
from litestar.types import Scope as LitestarScope
from litestar.types import Send as LitestarSend
from opentelemetry import baggage, propagate
from opentelemetry import context as otel_context
from opentelemetry.propagators.textmap import (
    Getter,
    TextMapPropagator,
    default_getter,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.status import (
    HTTP_200_OK,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from grelmicro import Grelmicro
from grelmicro.http import RateLimitedRequests
from grelmicro.integrations._request_telemetry import (
    RequestTelemetry,
    known_methods,
)
from grelmicro.metrics import Metrics, MetricsExporterType
from grelmicro.metrics._endpoints import render_prometheus
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.trace import Trace, TraceExporterType

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

    from grelmicro.trace._autoinstrument import InstrumentDirective


def _trace(*, instrument: InstrumentDirective = True) -> Trace:
    """Return a `Trace` that exports nowhere."""
    return Trace(exporter=TraceExporterType.NONE, instrument=instrument)


async def _read_item(request: Request) -> JSONResponse:
    return JSONResponse({"item_id": int(request.path_params["item_id"])})


def _starlette_items() -> Starlette:
    """Return a Starlette app serving `GET /v1/items/{item_id}`."""
    return Starlette(routes=[Route("/v1/items/{item_id:int}", _read_item)])


def _spans(
    micro: Grelmicro,
    app: Starlette,
    call: Callable[[TestClient], object],
) -> tuple[ReadableSpan, ...]:
    """Install `app`, make the calls inside its lifespan, return the spans."""
    exporter = InMemorySpanExporter()
    micro.install(app)
    with TestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        call(client)
        return exporter.get_finished_spans()


def _server_spans(spans: tuple[ReadableSpan, ...]) -> list[ReadableSpan]:
    return [span for span in spans if span.kind is SpanKind.SERVER]


def _metrics() -> Metrics:
    return Metrics(exporter=MetricsExporterType.PROMETHEUS)


def _request_durations(micro: Grelmicro) -> str:
    """Return the request duration series in the Prometheus exposition."""
    exposition = render_prometheus(micro.metrics).decode()
    return "\n".join(
        line
        for line in exposition.splitlines()
        if line.startswith("http_server_request_duration_seconds_count")
    )


async def _boom(_: Request) -> JSONResponse:
    msg = "boom"
    raise RuntimeError(msg)


async def _refused(_: Request) -> JSONResponse:
    raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR)


def _starlette_failing() -> Starlette:
    """Return a Starlette app whose routes crash or answer an error."""
    return Starlette(
        routes=[Route("/boom", _boom), Route("/refused", _refused)]
    )


def _server_span_of(
    micro: Grelmicro, app: Starlette, path: str
) -> ReadableSpan:
    exporter = InMemorySpanExporter()
    micro.install(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get(path)
    [server] = _server_spans(exporter.get_finished_spans())
    return server


def _exception_events(span: ReadableSpan) -> list[str]:
    return [
        str((event.attributes or {}).get("exception.type"))
        for event in span.events
        if event.name == "exception"
    ]


def _litestar_items() -> Litestar:
    """Return a Litestar app serving `GET /v1/items/{item_id}` from a router."""

    @get("/items/{item_id:int}")
    async def read_item(
        item_id: Annotated[int, Parameter()],
    ) -> dict[str, int]:
        return {"item_id": item_id}

    @get("/boom")
    async def boom() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    @get("/refused")
    async def refused() -> None:
        raise LitestarHTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR)

    return Litestar(
        route_handlers=[
            Router("/v1", route_handlers=[read_item, boom, refused])
        ]
    )


def _litestar_spans(
    micro: Grelmicro, app: Litestar, path: str
) -> list[ReadableSpan]:
    exporter = InMemorySpanExporter()
    micro.install(app)
    with LitestarTestClient(app, raise_server_exceptions=False) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get(path)
    return _server_spans(exporter.get_finished_spans())


class ItemError(Exception):
    """An exception of the app's own, outside the builtins."""


async def _chunks() -> AsyncIterator[bytes]:
    """Yield one chunk, then fail before the body is complete."""
    yield b"first"
    msg = "stream"
    raise ItemError(msg)


async def _raw(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer as the path says: wait for a disconnect, say nothing, or reply."""
    if scope["path"].endswith("/wait"):
        while (await receive())["type"] != "http.disconnect":
            pass
        return
    if scope["path"].endswith("/silent"):
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"raw"})


def _fastapi_parity_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/v1")

    @router.get("/items/{item_id}")
    def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @router.get("/boom")
    def boom() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    @router.get("/broken")
    def broken() -> None:
        msg = "broken"
        raise ItemError(msg)

    @router.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(_chunks())

    @router.websocket("/ws")
    async def crash(websocket: WebSocket) -> None:
        await websocket.accept()
        msg = "socket"
        raise RuntimeError(msg)

    app.include_router(router)
    app.mount("/v1/raw", _raw)
    return app


async def _broken(_: Request) -> JSONResponse:
    msg = "broken"
    raise ItemError(msg)


async def _stream(_: Request) -> StreamingResponse:
    return StreamingResponse(_chunks())


async def _socket_crash(websocket: WebSocket) -> None:
    await websocket.accept()
    msg = "socket"
    raise RuntimeError(msg)


def _starlette_parity_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/v1/items/{item_id:int}", _read_item),
            Route("/v1/boom", _boom),
            Route("/v1/broken", _broken),
            Route("/v1/stream", _stream),
            WebSocketRoute("/v1/ws", _socket_crash),
            Mount("/v1/raw", app=_raw),
        ]
    )


def _litestar_parity_app() -> Litestar:
    @get("/items/{item_id:int}")
    async def read_item(
        item_id: Annotated[int, Parameter()],
    ) -> dict[str, int]:
        return {"item_id": item_id}

    @get("/boom")
    async def boom() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    @get("/broken")
    async def broken() -> None:
        msg = "broken"
        raise ItemError(msg)

    @get("/stream")
    async def stream() -> Stream:
        return Stream(_chunks())

    @websocket("/ws")
    async def crash(socket: LitestarWebSocket) -> None:
        await socket.accept()
        msg = "socket"
        raise RuntimeError(msg)

    @asgi("/raw", is_mount=True, copy_scope=True)
    async def raw(
        scope: LitestarScope, receive: LitestarReceive, send: LitestarSend
    ) -> None:
        await _raw(scope, receive, send)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    return Litestar(
        route_handlers=[
            Router(
                "/v1",
                route_handlers=[read_item, boom, broken, stream, crash, raw],
            )
        ]
    )


_PARITY_APPS: dict[str, Callable[[], Any]] = {
    "fastapi": _fastapi_parity_app,
    "starlette": _starlette_parity_app,
    "litestar": _litestar_parity_app,
}

Recorded = tuple[str, dict[str, Any], list[str], list[str]]


def _active_requests(micro: Grelmicro) -> list[str]:
    """Return the active request series, labels and value."""
    return [
        re.sub(r'otel_scope_[a-z_]+="[^"]*",?', "", line)
        for line in render_prometheus(micro.metrics).decode().splitlines()
        if line.startswith("http_server_active_requests{")
    ]


def _what(micro: Grelmicro, exporter: InMemorySpanExporter) -> Recorded:
    """Return the span name, attributes, exception events and metric series.

    The duration series are read by their labels, the active request
    series by their labels and value.
    """
    [server] = _server_spans(exporter.get_finished_spans())
    labels = [
        re.sub(r'otel_scope_[a-z_]+="[^"]*",?', "", line).rsplit(" ", 1)[0]
        for line in _request_durations(micro).splitlines()
    ] + _active_requests(micro)
    return (
        server.name,
        dict(server.attributes or {}),
        _exception_events(server),
        labels,
    )


def _recorded(
    framework: str,
    method: str,
    path: str,
    *uses: Any,  # noqa: ANN401
) -> Recorded:
    """Return what one request through a test client records.

    `uses` adds components beside the trace and the metrics.
    """
    micro = Grelmicro(uses=[_trace(), _metrics(), *uses])
    app = _PARITY_APPS[framework]()
    exporter = InMemorySpanExporter()
    micro.install(app)
    client_type = LitestarTestClient if framework == "litestar" else TestClient
    with client_type(
        app, base_url="http://testserver.local", raise_server_exceptions=False
    ) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.request(method, path, follow_redirects=False)
        return _what(micro, exporter)


def _raw_recorded(
    framework: str, scope: dict[str, Any], messages: list[dict[str, Any]]
) -> Recorded:
    """Return what one request sent as a raw ASGI scope records."""
    micro = Grelmicro(uses=[_trace(), _metrics()])
    app = _PARITY_APPS[framework]()
    exporter = InMemorySpanExporter()
    micro.install(app)
    incoming = iter(messages)

    async def receive() -> dict[str, Any]:
        return next(incoming, {"type": "http.disconnect"})

    async def send(_: object) -> None:
        return None

    recorded: list[Recorded] = []

    async def run() -> None:
        async with micro:
            micro.trace.provider.add_span_processor(
                SimpleSpanProcessor(exporter)
            )
            with contextlib.suppress(Exception):
                await app(scope, receive, send)
            recorded.append(_what(micro, exporter))

    anyio.run(run)
    return recorded[0]


def _scope(path: str = "/v1/items/7", **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return an HTTP scope as a server sends it, with `changes` applied."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"api.example")],
        "server": ("10.0.0.1", 8080),
        "client": ("10.0.0.2", 5000),
    }
    scope.update(changes)
    return {key: value for key, value in scope.items() if value is not None}


_BODY: Final = [{"type": "http.request", "body": b"", "more_body": False}]
"""A request without a body, as the client sends it."""


class _HeaderNames(TextMapPropagator):
    """A propagator that reads which headers came in."""

    seen: ClassVar[list[str]] = []

    def extract(
        self,
        carrier: Any,  # noqa: ANN401
        context: otel_context.Context | None = None,
        getter: Getter[Any] = default_getter,
    ) -> otel_context.Context:
        type(self).seen = list(getter.keys(carrier))
        return context or otel_context.Context()

    def inject(self, *_: object, **__: object) -> None:
        return None

    @property
    def fields(self) -> set[str]:
        return set()


def _socket_recorded(framework: str) -> Recorded:
    """Return what a WebSocket whose handler crashes records."""
    micro = Grelmicro(uses=[_trace(), _metrics()])
    app = _PARITY_APPS[framework]()
    exporter = InMemorySpanExporter()
    micro.install(app)
    client_type = LitestarTestClient if framework == "litestar" else TestClient
    with client_type(
        app, base_url="http://testserver.local", raise_server_exceptions=False
    ) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        with (
            contextlib.suppress(Exception),
            client.websocket_connect("ws://testserver.local/v1/ws") as socket,
        ):
            socket.receive_text()
        [server] = _server_spans(exporter.get_finished_spans())
        return (
            server.name,
            dict(server.attributes or {}),
            _exception_events(server),
            [str(server.status.status_code)],
        )


def test_starlette_request_records_one_request_span() -> None:
    """The span is named by the route template, recorded by grelmicro."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])

    # Act
    spans = _spans(
        micro, _starlette_items(), lambda client: client.get("/v1/items/7")
    )

    # Assert
    [server] = _server_spans(spans)
    assert server.name == "GET /v1/items/{item_id}"
    assert server.attributes is not None
    assert server.attributes["http.route"] == "/v1/items/{item_id}"
    assert server.attributes["http.response.status_code"] == HTTP_200_OK
    assert server.instrumentation_scope is not None
    assert server.instrumentation_scope.name == "grelmicro.http"


def test_starlette_instrument_off_records_metrics_only() -> None:
    """`instrument={"starlette": False}` drops the span, not the metric."""
    # Arrange
    micro = Grelmicro(
        uses=[_trace(instrument={"starlette": False}), _metrics()]
    )
    app = _starlette_items()
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with TestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")
        durations = _request_durations(micro)

    # Assert
    assert _server_spans(exporter.get_finished_spans()) == []
    assert 'http_route="/v1/items/{item_id}"' in durations
    assert 'http_response_status_code="200"' in durations


def test_starlette_crash_records_exception_event() -> None:
    """The request span says what failed, and that it answered `500`."""
    # Act
    span = _server_span_of(
        Grelmicro(uses=[_trace()]), _starlette_failing(), "/boom"
    )

    # Assert
    assert span.status.status_code is StatusCode.ERROR
    assert _exception_events(span) == ["RuntimeError"]
    assert span.attributes is not None
    assert span.attributes["error.type"] == "RuntimeError"
    assert (
        span.attributes["http.response.status_code"]
        == HTTP_500_INTERNAL_SERVER_ERROR
    )


def test_starlette_answered_error_records_no_exception() -> None:
    """An error the app answers is not an exception on the span."""
    # Act
    span = _server_span_of(
        Grelmicro(uses=[_trace()]), _starlette_failing(), "/refused"
    )

    # Assert
    assert span.status.status_code is StatusCode.ERROR
    assert _exception_events(span) == []
    assert span.attributes is not None
    assert span.attributes["error.type"] == "500"


def test_starlette_exceptions_opted_into_logs_stay_off_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN=logs` keeps the event off."""
    # Arrange
    monkeypatch.setenv("OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN", "logs")

    # Act
    span = _server_span_of(
        Grelmicro(uses=[_trace()]), _starlette_failing(), "/boom"
    )

    # Assert
    assert _exception_events(span) == []
    assert span.attributes is not None
    assert span.attributes["error.type"] == "RuntimeError"


def test_starlette_mounted_fastapi_records_one_span_with_full_route() -> None:
    """The installed app records the request, FastAPI inside it does not."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])
    api = FastAPI()
    router = APIRouter(prefix="/v1")

    @router.get("/items/{item_id}")
    def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    api.include_router(router)
    app = Starlette(routes=[Mount("/api", app=api)])

    # Act
    spans = _spans(micro, app, lambda client: client.get("/api/v1/items/7"))

    # Assert
    [server] = _server_spans(spans)
    assert server.name == "GET /api/v1/items/{item_id}"
    assert server.instrumentation_scope is not None
    assert server.instrumentation_scope.name == "grelmicro.http"


def test_starlette_middleware_added_after_install_records_exception() -> None:
    """The event does not depend on when the app adds its middleware."""

    # Arrange
    class Failing:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(
            self, scope: Scope, receive: Receive, send: Send
        ) -> None:
            if scope["type"] == "http":
                msg = "boom"
                raise RuntimeError(msg)
            await self.app(scope, receive, send)

    micro = Grelmicro(uses=[_trace()])
    app = _starlette_items()
    exporter = InMemorySpanExporter()
    micro.install(app)
    app.add_middleware(Failing)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")

    # Assert
    [server] = _server_spans(exporter.get_finished_spans())
    assert _exception_events(server) == ["RuntimeError"]


@pytest.mark.parametrize(
    "variable",
    ["OTEL_PYTHON_STARLETTE_EXCLUDED_URLS", "OTEL_PYTHON_EXCLUDED_URLS"],
)
def test_starlette_excluded_url_records_nothing(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """A URL the environment excludes leaves no span and no measure."""
    # Arrange
    monkeypatch.setenv(variable, r"/v1/items/\d+$")
    micro = Grelmicro(uses=[_trace(), _metrics()])
    app = _starlette_items()
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with TestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7?full=1")
        durations = _request_durations(micro)

    # Assert
    assert _server_spans(exporter.get_finished_spans()) == []
    assert durations == ""


def test_starlette_excluded_urls_variable_replaces_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework's own list wins over the shared one."""
    # Arrange
    monkeypatch.setenv("OTEL_PYTHON_STARLETTE_EXCLUDED_URLS", "/healthz")
    monkeypatch.setenv("OTEL_PYTHON_EXCLUDED_URLS", "/v1/items")
    micro = Grelmicro(uses=[_trace()])

    # Act
    spans = _spans(
        micro, _starlette_items(), lambda client: client.get("/v1/items/7")
    )

    # Assert
    assert len(_server_spans(spans)) == 1


def test_starlette_websocket_records_one_span_named_by_route() -> None:
    """A WebSocket connection is traced like a request."""

    # Arrange
    async def chat(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text(websocket.path_params["room"])
        await websocket.close()

    micro = Grelmicro(uses=[_trace()])
    app = Starlette(routes=[WebSocketRoute("/ws/{room}", chat)])

    def connect(client: TestClient) -> None:
        with client.websocket_connect("/ws/lobby") as websocket:
            websocket.receive_text()

    # Act
    spans = _spans(micro, app, connect)

    # Assert
    [server] = _server_spans(spans)
    assert server.name == "WS /ws/{room}"
    assert _exception_events(server) == []


def test_starlette_incoming_traceparent_continues_trace() -> None:
    """A request carrying `traceparent` joins the caller's trace."""
    # Arrange
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    parent_id = "b7ad6b7169203331"
    micro = Grelmicro(uses=[_trace()])

    # Act
    spans = _spans(
        micro,
        _starlette_items(),
        lambda client: client.get(
            "/v1/items/7",
            headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
        ),
    )

    # Assert
    [server] = _server_spans(spans)
    assert server.context is not None
    assert server.parent is not None
    assert f"{server.context.trace_id:032x}" == trace_id
    assert f"{server.parent.span_id:016x}" == parent_id


def test_starlette_inactive_trace_records_no_span(
    monkeypatch: pytest.MonkeyPatch,
    global_spans: InMemorySpanExporter,
) -> None:
    """A `Trace` with no endpoint traces no request, whatever is global."""
    # Arrange
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    micro = Grelmicro(uses=[Trace()])
    app = _starlette_items()
    micro.install(app)

    # Act
    with TestClient(app) as client:
        status_code = client.get("/v1/items/7").status_code

    # Assert
    assert status_code == HTTP_200_OK
    assert _server_spans(global_spans.get_finished_spans()) == []


def test_starlette_metrics_alone_records_span_on_global_provider(
    global_spans: InMemorySpanExporter,
) -> None:
    """`Metrics` without `Trace` records the span on the app's own provider."""
    # Arrange
    micro = Grelmicro(uses=[_metrics()])
    app = _starlette_items()
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/v1/items/7")
        durations = _request_durations(micro)

    # Assert
    assert 'http_route="/v1/items/{item_id}"' in durations
    [server] = _server_spans(global_spans.get_finished_spans())
    assert server.name == "GET /v1/items/{item_id}"


def test_starlette_without_trace_or_metrics_records_nothing(
    global_spans: InMemorySpanExporter,
) -> None:
    """Nothing to export, nothing recorded."""
    # Arrange
    app = _starlette_items()
    Grelmicro().install(app)

    # Act
    with TestClient(app) as client:
        client.get("/v1/items/7")

    # Assert
    assert _server_spans(global_spans.get_finished_spans()) == []


def test_starlette_excluded_crash_answers_500_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An excluded request that crashes answers `500`, untraced."""
    # Arrange
    monkeypatch.setenv("OTEL_PYTHON_STARLETTE_EXCLUDED_URLS", "/boom")
    micro = Grelmicro(uses=[_trace()])
    app = _starlette_failing()
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        response = client.get("/boom")

    # Assert
    assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert _server_spans(exporter.get_finished_spans()) == []


def test_starlette_websocket_normal_close_records_no_exception() -> None:
    """A normal close is how a connection ends, not a failure."""

    # Arrange
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.receive_text()

    micro = Grelmicro(uses=[_trace()])
    app = Starlette(routes=[WebSocketRoute("/ws", echo)])

    def connect(client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws") as websocket,
        ):
            websocket.close(code=1000)

    # Act
    spans = _spans(micro, app, connect)

    # Assert
    [server] = _server_spans(spans)
    assert server.status.status_code is StatusCode.UNSET
    assert _exception_events(server) == []


def test_starlette_baggage_headers_reach_handler_whole() -> None:
    """Baggage split over several headers reaches the handler whole."""

    # Arrange
    async def read(_: Request) -> JSONResponse:
        return JSONResponse(dict(baggage.get_all()))

    micro = Grelmicro(uses=[_trace()])
    app = Starlette(routes=[Route("/baggage", read)])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.get(
            "/baggage", headers=[("baggage", "a=1"), ("baggage", "b=2")]
        )

    # Assert
    assert response.json() == {"a": "1", "b": "2"}


def test_starlette_propagator_reads_every_header_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A propagator that walks the headers sees each one the client sent."""
    # Arrange
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", _HeaderNames())
    micro = Grelmicro(uses=[_trace()])

    # Act
    _spans(
        micro,
        _starlette_items(),
        lambda client: client.get("/v1/items/7", headers={"x-tenant": "a"}),
    )

    # Assert
    assert "x-tenant" in _HeaderNames.seen


def test_starlette_requests_in_a_row_record_one_span_each() -> None:
    """Requests in a row each record a span and a measure."""
    # Arrange
    micro = Grelmicro(uses=[_trace(), _metrics()])
    app = _starlette_items()
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with TestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")
        client.get("/v1/items/8")
        durations = _request_durations(micro)

    # Assert
    assert len(_server_spans(exporter.get_finished_spans())) == 2  # noqa: PLR2004
    assert durations.endswith(" 2.0")


def test_litestar_request_records_one_request_span() -> None:
    """The span is named by the route template, router prefix included."""
    # Act
    [server] = _litestar_spans(
        Grelmicro(uses=[_trace()]), _litestar_items(), "/v1/items/7"
    )

    # Assert
    assert server.name == "GET /v1/items/{item_id}"
    assert server.attributes is not None
    assert server.attributes["http.route"] == "/v1/items/{item_id}"
    assert server.attributes["http.response.status_code"] == HTTP_200_OK
    assert server.instrumentation_scope is not None
    assert server.instrumentation_scope.name == "grelmicro.http"


def test_litestar_crash_records_exception_event() -> None:
    """Litestar answers the crash itself, and the span still says what failed."""
    # Act
    [server] = _litestar_spans(
        Grelmicro(uses=[_trace()]), _litestar_items(), "/v1/boom"
    )

    # Assert
    assert server.status.status_code is StatusCode.ERROR
    assert _exception_events(server) == ["RuntimeError"]
    assert server.attributes is not None
    assert server.attributes["error.type"] == "RuntimeError"
    assert (
        server.attributes["http.response.status_code"]
        == HTTP_500_INTERNAL_SERVER_ERROR
    )


def test_litestar_answered_error_records_no_exception() -> None:
    """An `HTTPException` is an answer, not an exception on the span."""
    # Act
    [server] = _litestar_spans(
        Grelmicro(uses=[_trace()]), _litestar_items(), "/v1/refused"
    )

    # Assert
    assert _exception_events(server) == []
    assert server.attributes is not None
    assert server.attributes["error.type"] == "500"


def test_litestar_unmatched_request_records_status_without_route() -> None:
    """A `404` has no route and is no failure."""
    # Act
    [server] = _litestar_spans(
        Grelmicro(uses=[_trace()]), _litestar_items(), "/nowhere"
    )

    # Assert
    assert server.name == "GET"
    assert server.status.status_code is StatusCode.UNSET
    assert server.attributes is not None
    assert server.attributes["http.response.status_code"] == HTTP_404_NOT_FOUND


def test_litestar_instrument_off_records_metrics_only() -> None:
    """`instrument={"litestar": False}` drops the span, not the metric."""
    # Arrange
    micro = Grelmicro(uses=[_trace(instrument={"litestar": False}), _metrics()])
    app = _litestar_items()
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with LitestarTestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")
        durations = _request_durations(micro)

    # Assert
    assert _server_spans(exporter.get_finished_spans()) == []
    assert 'http_route="/v1/items/{item_id}"' in durations


def test_litestar_excluded_url_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OTEL_PYTHON_LITESTAR_EXCLUDED_URLS` names the URLs to leave out."""
    # Arrange
    monkeypatch.setenv("OTEL_PYTHON_LITESTAR_EXCLUDED_URLS", "/v1/items")

    # Act
    spans = _litestar_spans(
        Grelmicro(uses=[_trace()]), _litestar_items(), "/v1/items/7"
    )

    # Assert
    assert spans == []


@pytest.mark.parametrize("code", [1000, 1001])
def test_litestar_websocket_normal_close_records_no_exception(
    code: int,
) -> None:
    """A normal close is how a connection ends, not a failure."""

    # Arrange
    @websocket("/ws")
    async def echo(socket: LitestarWebSocket) -> None:
        await socket.accept()
        await socket.receive_text()

    micro = Grelmicro(uses=[_trace()])
    app = Litestar(route_handlers=[echo])
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with LitestarTestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        with client.websocket_connect("/ws") as socket:
            socket.close(code=code)

    # Assert
    [server] = _server_spans(exporter.get_finished_spans())
    assert server.status.status_code is StatusCode.UNSET
    assert _exception_events(server) == []


def test_litestar_websocket_records_one_span_named_by_route() -> None:
    """A WebSocket connection is traced like a request."""

    # Arrange
    @websocket("/ws/{room:str}")
    async def chat(
        socket: LitestarWebSocket, room: Annotated[str, Parameter()]
    ) -> None:
        await socket.accept()
        await socket.send_text(room)
        await socket.close()

    micro = Grelmicro(uses=[_trace()])
    app = Litestar(route_handlers=[chat])
    exporter = InMemorySpanExporter()
    micro.install(app)

    # Act
    with LitestarTestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        with client.websocket_connect("/ws/lobby") as socket:
            socket.receive_text()

    # Assert
    [server] = _server_spans(exporter.get_finished_spans())
    assert server.name == "WS /ws/{room}"
    assert _exception_events(server) == []


def test_litestar_error_caught_on_purpose_records_no_exception() -> None:
    """An exception the route catches on purpose is an answer."""

    # Arrange
    def answer(_: LitestarRequest, __: ItemError) -> LitestarResponseType:
        return LitestarResponseType(content="nope", status_code=422)

    @get("/broken", exception_handlers={ItemError: answer})
    async def broken() -> None:
        msg = "broken"
        raise ItemError(msg)

    # Act
    [server] = _litestar_spans(
        Grelmicro(uses=[_trace()]), Litestar(route_handlers=[broken]), "/broken"
    )

    # Assert
    assert _exception_events(server) == []
    assert server.status.status_code is StatusCode.UNSET


@pytest.mark.parametrize("framework", ["starlette", "litestar"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/items/7"),
        ("GET", "/v1/items/7?sig=secret&page=2"),
        ("GET", "/v1/boom"),
        ("GET", "/v1/broken"),
        ("GET", "/v1/stream"),
        ("GET", "/v1/raw/files/a.txt"),
        ("GET", "/nowhere"),
        ("FOO", "/v1/items/7"),
        ("POST", "/v1/boom"),
    ],
    ids=[
        "ok",
        "redacted-query",
        "crash",
        "crash-of-the-app",
        "stream-failing-midway",
        "mount",
        "not-found",
        "unknown-method",
        "method-not-allowed",
    ],
)
def test_request_telemetry_request_matches_fastapi(
    framework: str, method: str, path: str
) -> None:
    """Same span name, attributes, events and metric labels as FastAPI."""
    # Act
    recorded = _recorded(framework, method, path)

    # Assert
    assert recorded == _recorded("fastapi", method, path)


def _limited() -> RateLimitedRequests:
    """Return a rate limit that answers nothing, for a request to pass through."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "parity", limit=100, window=60, backend=MemoryRateLimiterAdapter()
        ),
        key=lambda scope: "one caller",  # noqa: ARG005
    )


@pytest.mark.parametrize("framework", ["starlette", "litestar"])
@pytest.mark.parametrize(
    "path",
    ["/v1/boom", "/v1/broken", "/v1/stream"],
    ids=["crash", "crash-of-the-app", "stream-failing-midway"],
)
def test_request_telemetry_under_an_answering_middleware_matches_fastapi(
    framework: str, path: str
) -> None:
    """A middleware of ours that answers leaves the recorded failure as it is."""
    # Act
    recorded = _recorded(framework, "GET", path, _limited())

    # Assert
    assert recorded == _recorded("fastapi", "GET", path, _limited())


@pytest.mark.parametrize("framework", ["starlette", "litestar"])
@pytest.mark.parametrize(
    ("scope", "messages"),
    [
        (_scope(headers=[]), _BODY),
        (_scope(server=None, headers=[]), _BODY),
        (_scope(headers=[(b"host", b"api.example:8443")]), _BODY),
        (_scope(headers=[(b"host", b"user@api.example")]), _BODY),
        (_scope(server=("10.0.0.1", None), headers=[]), _BODY),
        (_scope(scheme="https"), _BODY),
        (_scope(http_version=None), _BODY),
        (_scope("/api/v1/items/7", root_path="/api"), _BODY),
    ],
    ids=[
        "no-host",
        "no-host-no-server",
        "host-with-port",
        "host-with-user",
        "server-without-port",
        "https",
        "no-http-version",
        "root-path",
    ],
)
def test_request_telemetry_raw_request_matches_fastapi(
    framework: str, scope: dict[str, Any], messages: list[dict[str, Any]]
) -> None:
    """Scopes a test client cannot send record as they do on FastAPI."""
    # Act
    recorded = _raw_recorded(framework, dict(scope), messages)

    # Assert
    assert recorded == _raw_recorded("fastapi", dict(scope), messages)


def _active_in_flight(framework: str) -> list[str]:
    """Return the active request series as the response starts."""
    micro = Grelmicro(uses=[_metrics()])
    app = _PARITY_APPS[framework]()
    micro.install(app)
    incoming = iter(_BODY)
    seen: list[list[str]] = []

    async def receive() -> dict[str, Any]:
        return next(incoming, {"type": "http.disconnect"})

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            seen.append(_active_requests(micro))

    async def run() -> None:
        async with micro:
            await app(_scope(), receive, send)

    anyio.run(run)
    return seen[0]


@pytest.mark.parametrize("framework", ["fastapi", "starlette", "litestar"])
def test_request_telemetry_counts_a_request_in_flight(framework: str) -> None:
    """A request is active until it is answered, with FastAPI's attributes."""
    # Act
    active = _active_in_flight(framework)

    # Assert
    assert active == [
        (
            'http_server_active_requests{http_request_method="GET",'
            'url_scheme="http"} 1.0'
        )
    ]


@pytest.mark.parametrize(
    ("scope", "messages"),
    [
        (_scope("/v1/raw/wait"), []),
        (_scope("/v1/raw/silent"), _BODY),
        (_scope(headers=[(b"host", b"[::1")]), _BODY),
    ],
    ids=["client-disconnected", "no-answer", "malformed-host"],
)
def test_starlette_raw_request_matches_fastapi(
    scope: dict[str, Any], messages: list[dict[str, Any]]
) -> None:
    """Requests Litestar answers itself record on Starlette as on FastAPI.

    Litestar answers an unanswered mount with a `200`, and a malformed
    `Host` with a `400`, before any route.
    """
    # Act
    recorded = _raw_recorded("starlette", dict(scope), messages)

    # Assert
    assert recorded == _raw_recorded("fastapi", dict(scope), messages)


@pytest.mark.parametrize("framework", ["starlette", "litestar"])
def test_request_telemetry_two_grelmicros_record_once(framework: str) -> None:
    """A second `Grelmicro` does not record the request a second time."""
    # Arrange
    first = Grelmicro(uses=[_trace()])
    second = Grelmicro(uses=[_trace()], allow_multiple=True)
    app = _PARITY_APPS[framework]()
    exporter = InMemorySpanExporter()
    first.install(app)
    second.install(app)
    client_type = LitestarTestClient if framework == "litestar" else TestClient

    # Act
    with client_type(app) as client:
        second.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        first.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")

    # Assert
    assert len(_server_spans(exporter.get_finished_spans())) == 1


@pytest.mark.parametrize("framework", ["starlette", "litestar"])
def test_request_telemetry_without_ambient_binding_records_span(
    framework: str,
) -> None:
    """The request is recorded whether or not the binding is installed."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])
    app = _PARITY_APPS[framework]()
    exporter = InMemorySpanExporter()
    micro.install(app, ambient=False)
    client_type = LitestarTestClient if framework == "litestar" else TestClient

    # Act
    with client_type(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")

    # Assert
    [server] = _server_spans(exporter.get_finished_spans())
    assert server.name == "GET /v1/items/{item_id}"


@pytest.mark.parametrize("framework", ["starlette", "litestar"])
def test_request_telemetry_websocket_crash_matches_fastapi(
    framework: str,
) -> None:
    """A WebSocket whose handler crashes fails its span, as on FastAPI."""
    # Act
    recorded = _socket_recorded(framework)

    # Assert
    assert recorded == _socket_recorded("fastapi")


def test_starlette_slash_redirect_matches_fastapi() -> None:
    """A `307` to the slashless path names the route it redirects to."""
    # Act
    recorded = _recorded("starlette", "GET", "/v1/items/7/")

    # Assert
    assert recorded == _recorded("fastapi", "GET", "/v1/items/7/")


@pytest.mark.parametrize(
    ("redirect_slashes", "path"),
    [(False, "/v1/items/7/"), (True, "/")],
    ids=["redirects-off", "root"],
)
def test_starlette_unredirected_unknown_path_records_no_route(
    redirect_slashes: bool,  # noqa: FBT001
    path: str,
) -> None:
    """A `404` the router does not redirect names no route."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])
    app = _starlette_items()
    app.router.redirect_slashes = redirect_slashes

    # Act
    spans = _spans(micro, app, lambda client: client.get(path))

    # Assert
    [server] = _server_spans(spans)
    assert server.name == "GET"
    assert server.attributes is not None
    assert "http.route" not in server.attributes


def test_starlette_unknown_websocket_records_no_route() -> None:
    """A WebSocket no route answers names no route."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])

    def connect(client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/v1/items/7/"),
        ):
            pass

    # Act
    spans = _spans(micro, _starlette_items(), connect)

    # Assert
    [server] = _server_spans(spans)
    assert server.name == "WS"


def test_litestar_cors_preflight_records_no_route_and_succeeds() -> None:
    """A preflight Litestar answers before routing is recorded, unrouted."""

    # Arrange
    @get("/v1/items")
    async def items() -> list[int]:
        return [7]

    micro = Grelmicro(uses=[_trace(), _metrics()])
    cors = Litestar(
        route_handlers=[items],
        cors_config=CORSConfig(allow_origins=["https://app.example"]),
    )
    exporter = InMemorySpanExporter()
    micro.install(cors)

    # Act
    with LitestarTestClient(cors) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        response = client.options(
            "/v1/items",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        active = render_prometheus(micro.metrics).decode()

    # Assert
    assert response.status_code == HTTP_204_NO_CONTENT
    [server] = _server_spans(exporter.get_finished_spans())
    assert server.name == "OPTIONS"
    [series] = [
        line
        for line in active.splitlines()
        if line.startswith("http_server_active_requests{")
    ]
    assert series.endswith(" 0.0")


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_litestar_invalid_host_records_status_without_route(
    method: str,
) -> None:
    """A request Litestar refuses before routing names no route."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])

    # Act
    spans = _litestar_raw_spans(
        micro,
        _litestar_items(),
        _scope(method=method, headers=[(b"host", b"bad host")]),
    )

    # Assert
    [server] = spans
    assert server.name == method
    assert server.attributes is not None
    assert "http.route" not in server.attributes
    assert (
        server.attributes["http.response.status_code"] == HTTP_400_BAD_REQUEST
    )


def test_request_telemetry_failing_route_reader_still_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A route that cannot be read leaves the span ended, unnamed, and logs why."""
    # Arrange
    micro = Grelmicro(uses=[_trace()])
    exporter = InMemorySpanExporter()

    def unreadable(
        _scope: Scope, _root_path: str, _path: str, _status: int | None
    ) -> str | None:
        msg = "unreadable"
        raise LookupError(msg)

    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    app = RequestTelemetry(
        ok,
        route=unreadable,
        tracing=True,
        exclude=None,
        methods=known_methods(),
        events=True,
    )

    sent: list[dict[str, Any]] = []
    messages = iter(_BODY)

    async def receive() -> dict[str, Any]:
        return next(messages, {"type": "http.disconnect"})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def run() -> None:
        async with micro:
            micro.trace.provider.add_span_processor(
                SimpleSpanProcessor(exporter)
            )
            await app(_scope("/anything"), receive, send)  # ty: ignore[invalid-argument-type]

    # Act
    anyio.run(run)

    # Assert
    assert sent[0]["status"] == HTTP_200_OK
    [server] = _server_spans(exporter.get_finished_spans())
    assert server.name == "GET"
    assert "Could not read the route of /anything" in caplog.text


def _litestar_raw_spans(
    micro: Grelmicro, app: Litestar, scope: dict[str, Any]
) -> list[ReadableSpan]:
    """Return the request spans one raw scope sent to `app` records."""
    exporter = InMemorySpanExporter()
    micro.install(app)
    messages = iter(_BODY)

    async def receive() -> dict[str, Any]:
        return next(messages, {"type": "http.disconnect"})

    async def send(_: object) -> None:
        return None

    async def run() -> None:
        async with micro:
            micro.trace.provider.add_span_processor(
                SimpleSpanProcessor(exporter)
            )
            await app(scope, receive, send)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    anyio.run(run)
    return _server_spans(exporter.get_finished_spans())
