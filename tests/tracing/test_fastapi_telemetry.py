"""FastAPI request telemetry, as `micro.install(app)` wires it.

FastAPI records the request spans and the HTTP server metrics. grelmicro
exports them, decides which requests are traced, and adds the exception
event to the server span.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.telemetry import _runtime
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry._logs import _internal as logs_internal
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
from starlette.status import HTTP_200_OK, HTTP_500_INTERNAL_SERVER_ERROR

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsExporterType
from grelmicro.metrics._endpoints import render_prometheus
from grelmicro.trace import Trace, TraceExporterType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi.telemetry import TelemetryConfig
    from opentelemetry.sdk.trace import ReadableSpan
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from grelmicro.trace._autoinstrument import InstrumentDirective


def _trace(*, instrument: InstrumentDirective = True) -> Trace:
    """Return a `Trace` that exports nowhere."""
    return Trace(exporter=TraceExporterType.NONE, instrument=instrument)


def _items_app(telemetry: TelemetryConfig | None = None) -> FastAPI:
    """Return an app serving `GET /v1/items/{item_id}` from a router."""
    app = FastAPI(telemetry=telemetry)
    router = APIRouter(prefix="/v1")

    @router.get("/items/{item_id}")
    def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    app.include_router(router)
    return app


def _spans(
    micro: Grelmicro,
    app: FastAPI,
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


def test_a_request_is_one_server_span_from_fastapi() -> None:
    """FastAPI records the span, named by the route with its prefixes."""
    micro = Grelmicro(uses=[_trace()])

    spans = _spans(
        micro, _items_app(), lambda client: client.get("/v1/items/7")
    )

    [server] = _server_spans(spans)
    assert server.name == "GET /v1/items/{item_id}"
    assert server.attributes is not None
    assert server.attributes["http.route"] == "/v1/items/{item_id}"
    assert server.instrumentation_scope is not None
    assert server.instrumentation_scope.name == "fastapi"
    children = {span.name for span in spans if span.parent is not None}
    assert children == {
        "fastapi.dependencies",
        "fastapi.endpoint",
        "fastapi.serialization",
    }


@pytest.fixture
def fastapi_pipelines(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[object]]:
    """Return what FastAPI set up from the environment, and tear it down.

    FastAPI starts with no global providers, so it sets up its own, and the
    ones before are put back.
    """
    for module, name, once in (
        (trace, "_TRACER_PROVIDER", trace._TRACER_PROVIDER_SET_ONCE),
        (
            metrics_internal,
            "_METER_PROVIDER",
            metrics_internal._METER_PROVIDER_SET_ONCE,
        ),
        (
            logs_internal,
            "_LOGGER_PROVIDER",
            logs_internal._LOGGER_PROVIDER_SET_ONCE,
        ),
    ):
        monkeypatch.setattr(module, name, None)
        monkeypatch.setattr(once, "_done", False)
    yield _runtime._owned
    _runtime._shutdown()
    _runtime._configured.clear()


@pytest.mark.parametrize(
    "environment",
    [
        {"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"},
        {"OTEL_TRACES_EXPORTER": "console"},
        {},
    ],
    ids=["grpc", "console", "http"],
)
def test_fastapi_builds_no_pipeline_beside_grelmicro(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fastapi_pipelines: list[object],
    environment: dict[str, str],
) -> None:
    """The app starts quietly, and every signal goes through grelmicro."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    micro = Grelmicro(
        uses=[_trace(), Metrics(exporter=MetricsExporterType.PROMETHEUS)]
    )
    app = _items_app()
    micro.install(app)

    with TestClient(app) as client:
        assert client.get("/v1/items/7").status_code == HTTP_200_OK
        assert trace.get_tracer_provider() is micro.trace.provider
        assert metrics.get_meter_provider() is micro.metrics.provider

    assert fastapi_pipelines == []
    assert not [r for r in caplog.records if r.name.startswith("fastapi")]


def test_fastapi_sets_up_its_own_export_without_grelmicro(
    monkeypatch: pytest.MonkeyPatch,
    fastapi_pipelines: list[object],
) -> None:
    """Without `Trace` or `Metrics`, FastAPI reads the environment as usual."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    app = _items_app()
    Grelmicro().install(app)

    with TestClient(app):
        pass

    assert fastapi_pipelines != []


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


def test_untraced_fastapi_still_records_request_metrics() -> None:
    """`instrument={"fastapi": False}` drops the span, not the metric."""
    micro = Grelmicro(uses=[_trace(instrument={"fastapi": False}), _metrics()])
    app = _items_app()
    exporter = InMemorySpanExporter()
    micro.install(app)

    with TestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/v1/items/7")
        durations = _request_durations(micro)

    assert _server_spans(exporter.get_finished_spans()) == []
    assert 'http_route="/v1/items/{item_id}"' in durations


def test_metrics_alone_keeps_fastapi_from_exporting(
    monkeypatch: pytest.MonkeyPatch,
    fastapi_pipelines: list[object],
    global_spans: InMemorySpanExporter,
) -> None:
    """`Metrics` without `Trace` owns export, and spans go to the global provider."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    micro = Grelmicro(uses=[_metrics()])
    app = _items_app()
    micro.install(app)

    with TestClient(app) as client:
        client.get("/v1/items/7")
        durations = _request_durations(micro)

    assert fastapi_pipelines == []
    assert 'http_route="/v1/items/{item_id}"' in durations
    [server] = _server_spans(global_spans.get_finished_spans())
    assert server.name == "GET /v1/items/{item_id}"


def test_an_inactive_trace_turns_request_spans_off(
    monkeypatch: pytest.MonkeyPatch,
    global_spans: InMemorySpanExporter,
) -> None:
    """A `Trace` with no endpoint traces no request, whatever is global."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    micro = Grelmicro(uses=[Trace()])
    app = _items_app()
    micro.install(app)

    with TestClient(app) as client:
        assert client.get("/v1/items/7").status_code == HTTP_200_OK

    assert _server_spans(global_spans.get_finished_spans()) == []


def _failing_app() -> FastAPI:
    """Return an app whose routes fail in each way a request can."""
    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    @app.get("/refused")
    def refused() -> None:
        raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR)

    @app.get("/count/{n}")
    def count(n: int) -> dict[str, int]:
        return {"n": n}

    return app


def _server_span_of(micro: Grelmicro, app: FastAPI, path: str) -> ReadableSpan:
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


def test_an_unhandled_exception_is_an_event_on_the_server_span() -> None:
    """The server span says what failed, not only that it failed."""
    span = _server_span_of(Grelmicro(uses=[_trace()]), _failing_app(), "/boom")

    assert span.status.status_code is StatusCode.ERROR
    assert _exception_events(span) == ["RuntimeError"]


@pytest.mark.parametrize(
    ("path", "status"),
    [("/refused", StatusCode.ERROR), ("/count/x", StatusCode.UNSET)],
    ids=["http-exception", "validation"],
)
def test_an_answered_error_records_no_exception(
    path: str, status: StatusCode
) -> None:
    """An error the app answers is not an exception on the span."""
    span = _server_span_of(Grelmicro(uses=[_trace()]), _failing_app(), path)

    assert span.status.status_code is status
    assert _exception_events(span) == []


def test_middleware_added_after_install_reports_its_exception() -> None:
    """The event does not depend on when the app adds its middleware."""

    class Failing:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(
            self, scope: Scope, receive: Receive, send: Send
        ) -> None:
            if scope["type"] == "http":
                msg = "middleware"
                raise LookupError(msg)
            await self.app(scope, receive, send)

    micro = Grelmicro(uses=[_trace()])
    app = _failing_app()
    micro.install(app)
    app.add_middleware(Failing)
    exporter = InMemorySpanExporter()
    with TestClient(app, raise_server_exceptions=False) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        client.get("/count/1")

    [server] = _server_spans(exporter.get_finished_spans())
    assert _exception_events(server) == ["LookupError"]


def test_exceptions_opted_into_logs_leave_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN=logs` records no span event."""
    monkeypatch.setenv("OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN", "logs")
    span = _server_span_of(Grelmicro(uses=[_trace()]), _failing_app(), "/boom")

    assert span.status.status_code is StatusCode.ERROR
    assert _exception_events(span) == []


def _traced_paths(
    micro: Grelmicro, app: FastAPI, paths: list[str]
) -> tuple[set[str], str]:
    """Request each path, return the traced URL paths and the durations."""
    exporter = InMemorySpanExporter()
    micro.install(app)
    with TestClient(app) as client:
        micro.trace.provider.add_span_processor(SimpleSpanProcessor(exporter))
        for path in paths:
            client.get(path)
        durations = _request_durations(micro)
    traced = {
        str((span.attributes or {})["url.path"])
        for span in _server_spans(exporter.get_finished_spans())
    }
    return traced, durations


def test_excluded_urls_are_neither_traced_nor_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL matching `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` leaves no trace."""
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "items/7, health")
    micro = Grelmicro(uses=[_trace(), _metrics()])

    traced, durations = _traced_paths(
        micro, _items_app(), ["/v1/items/7", "/v1/items/8"]
    )

    assert traced == {"/v1/items/8"}
    [series] = durations.splitlines()
    assert series.endswith(" 1.0")


def test_the_fastapi_variable_replaces_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OTEL_PYTHON_EXCLUDED_URLS` applies only when the FastAPI one is unset."""
    monkeypatch.setenv("OTEL_PYTHON_EXCLUDED_URLS", "items/8")
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "items/7")
    micro = Grelmicro(uses=[_trace(), _metrics()])

    traced, _ = _traced_paths(
        micro, _items_app(), ["/v1/items/7", "/v1/items/8"]
    )

    assert traced == {"/v1/items/8"}


def test_the_apps_own_exclusion_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FastAPI(telemetry={"exclude": ...})` runs once per request beside them."""
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "items/7")
    seen: list[str] = []

    def exclude(scope: Scope) -> bool:
        seen.append(scope["path"])
        return scope["path"] == "/v1/items/9"

    micro = Grelmicro(uses=[_trace(), _metrics()])

    traced, _ = _traced_paths(
        micro,
        _items_app({"exclude": exclude}),
        ["/v1/items/7", "/v1/items/8", "/v1/items/9"],
    )

    assert traced == {"/v1/items/8"}
    assert seen == ["/v1/items/7", "/v1/items/8", "/v1/items/9"]


def test_the_apps_own_telemetry_settings_are_kept() -> None:
    """Grelmicro only turns things off, and leaves the app's choices alone."""
    micro = Grelmicro(uses=[_trace()])
    app = _items_app({"operation_spans": False})

    spans = _spans(micro, app, lambda client: client.get("/v1/items/7"))

    [server] = _server_spans(spans)
    assert [span for span in spans if span is not server] == []


def test_install_leaves_the_apps_other_signals_alone() -> None:
    """Settings grelmicro does not own keep the app's values."""
    micro = Grelmicro(uses=[_trace(), _metrics()])
    app = _items_app(
        {"logs": False, "metrics": False, "operation_spans": False}
    )

    micro.install(app)

    assert app._telemetry["logs"] is False
    assert app._telemetry["metrics"] is False
    assert app._telemetry["operation_spans"] is False


def test_an_app_that_turned_tracing_off_stays_untraced() -> None:
    """`FastAPI(telemetry={"tracing": False})` wins over `Trace`."""
    micro = Grelmicro(uses=[_trace()])
    app = _items_app({"tracing": False})

    spans = _spans(micro, app, lambda client: client.get("/v1/items/7"))

    assert _server_spans(spans) == []


def test_a_mounted_app_is_one_span_named_by_its_full_route() -> None:
    """The outer app traces the request, through the mount."""
    micro = Grelmicro(uses=[_trace()])
    app = FastAPI()
    app.mount("/sub", _items_app())

    spans = _spans(micro, app, lambda client: client.get("/sub/v1/items/7"))

    [server] = _server_spans(spans)
    assert server.name == "GET /sub/v1/items/{item_id}"


def test_an_incoming_trace_is_continued() -> None:
    """A request carrying `traceparent` joins the caller's trace."""
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    parent_id = "b7ad6b7169203331"
    micro = Grelmicro(uses=[_trace()])

    spans = _spans(
        micro,
        _items_app(),
        lambda client: client.get(
            "/v1/items/7",
            headers={"traceparent": f"00-{trace_id}-{parent_id}-01"},
        ),
    )

    [server] = _server_spans(spans)
    assert server.context is not None
    assert server.parent is not None
    assert f"{server.context.trace_id:032x}" == trace_id
    assert f"{server.parent.span_id:016x}" == parent_id


def test_a_websocket_is_one_span_named_by_its_route() -> None:
    """A WebSocket connection is traced like a request."""
    micro = Grelmicro(uses=[_trace()])
    app = FastAPI()

    @app.websocket("/ws/{room}")
    async def chat(websocket: WebSocket, room: str) -> None:
        await websocket.accept()
        await websocket.send_text(room)
        await websocket.close()

    def connect(client: TestClient) -> None:
        with client.websocket_connect("/ws/lobby") as websocket:
            websocket.receive_text()

    spans = _spans(micro, app, connect)

    [server] = _server_spans(spans)
    assert server.name == "WS /ws/{room}"
    assert _exception_events(server) == []


def _replace_settings(app: FastAPI) -> None:
    app._telemetry = app._native_telemetry.config = ()  # type: ignore[assignment]  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "move",
    [
        lambda app: delattr(app, "_telemetry"),
        lambda app: delattr(app, "_native_telemetry"),
        _replace_settings,
    ],
    ids=["settings", "reader", "not-a-dict"],
)
def test_install_refuses_a_fastapi_without_telemetry_settings(
    move: Callable[[FastAPI], None],
) -> None:
    """A FastAPI release that moved its settings fails at install."""
    app = _items_app()
    move(app)

    with pytest.raises(RuntimeError, match="_telemetry"):
        Grelmicro(uses=[_trace()], allow_multiple=True).install(app)


def test_two_grelmicros_on_one_app_exclude_and_record_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first install wires the telemetry, and a second adds nothing."""
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "items/7")
    seen: list[str] = []

    def exclude(scope: Scope) -> bool:
        seen.append(scope["path"])
        return False

    app = _failing_app()
    app._telemetry["exclude"] = exclude
    Grelmicro(uses=[_trace()]).install(app)
    second = Grelmicro(uses=[_trace()], allow_multiple=True)

    span = _server_span_of(second, app, "/boom")

    assert seen == ["/boom"]
    assert _exception_events(span) == ["RuntimeError"]


@pytest.mark.parametrize(
    ("server", "excluded"),
    [
        (("internal", 8080), "//internal:8080/v1"),
        (("internal", 80), "//internal/v1"),
    ],
    ids=["port", "default-port"],
)
def test_a_request_without_host_is_excluded_by_its_server_address(
    monkeypatch: pytest.MonkeyPatch,
    server: tuple[str, int],
    excluded: str,
) -> None:
    """Without a `Host` header, the URL names the address that served it."""
    monkeypatch.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", excluded)
    micro = Grelmicro(uses=[_trace()])
    app = _items_app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/items/7",
        "raw_path": b"/v1/items/7",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "server": server,
        "client": ("192.0.2.1", 50000),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    def call(client: TestClient) -> None:
        assert client.portal is not None
        client.portal.call(app, scope, receive, send)

    spans = _spans(micro, app, call)

    assert sent[0]["status"] == HTTP_200_OK
    assert _server_spans(spans) == []


def test_a_websocket_the_client_closes_records_no_exception() -> None:
    """A normal close is how a connection ends, not a failure."""
    micro = Grelmicro(uses=[_trace()])
    app = FastAPI()

    @app.websocket("/ws")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.receive_text()

    def connect(client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws") as websocket,
        ):
            websocket.close(code=1000)

    spans = _spans(micro, app, connect)

    [server] = _server_spans(spans)
    assert _exception_events(server) == []
