"""Request telemetry for Starlette and Litestar: the request span and the HTTP server metrics.

`RequestTelemetry` records them through the global tracer and meter
providers that `Trace` and `Metrics` install, following the HTTP semantic
conventions 1.44. Each framework hands it a way to read the route template
the request matched. Nothing here imports a web framework.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import nullcontext
from importlib.metadata import version
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit

from opentelemetry import context as otel_context
from opentelemetry import metrics, propagate, trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import SpanKind, StatusCode

from grelmicro._paths import Answered
from grelmicro.integrations._fastapi_internals import TELEMETRY_KEY

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from opentelemetry.metrics import Histogram, UpDownCounter
    from opentelemetry.trace import Span, Tracer

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
    RouteOf = Callable[[Scope, "Answered"], "str | None"]

__all__ = [
    "SCOPE_NAME",
    "Answered",
    "RaisedExceptions",
    "RequestTelemetry",
    "exceptions_on_spans",
    "excluding",
    "known_methods",
    "normal_close",
    "record_unhandled",
]

_logger = logging.getLogger(__name__)


SCOPE_NAME: Final = "grelmicro.http"
"""The instrumentation scope of the request spans and the HTTP server metrics."""

_KEY: Final = "grelmicro.request_telemetry"
"""Where a request being recorded holds its telemetry."""

_SCHEMA_URL: Final = "https://opentelemetry.io/schemas/1.44.0"
"""The semantic conventions version the telemetry follows."""

_DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)
"""The bucket boundaries of `http.server.request.duration`, in seconds."""

_HTTP_METHODS: Final = frozenset(
    {
        "CONNECT",
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "QUERY",
        "TRACE",
    }
)
"""The methods recorded as they are. Any other one is recorded as `_OTHER`."""

_SENSITIVE_QUERY_PARAMETERS: Final = frozenset(
    {
        "X-Amz-Signature",
        "X-Amz-Credential",
        "X-Amz-Security-Token",
        "sig",
        "X-Goog-Signature",
    }
)
"""Query parameters whose value `url.query` records as `REDACTED`."""

_DEFAULT_PORTS: Final = {"http": 80, "https": 443, "ws": 80, "wss": 443}
"""The port a URL of each scheme leaves out."""

_DEFERRED_PROVIDERS: Final = frozenset(
    {
        ("opentelemetry.trace", "ProxyTracerProvider"),
        ("opentelemetry.metrics._internal", "_ProxyMeterProvider"),
    }
)
"""The global providers OpenTelemetry stands in with until one is set."""

_NORMAL_CLOSE: Final = frozenset({1000, 1001})
"""WebSocket close codes for a connection that ended as it should."""

_VERSION: Final = version("grelmicro")
"""The instrumentation scope version."""


def exceptions_on_spans() -> bool:
    """Return whether exceptions go on spans, per `OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN`.

    `logs` moves them to the logs signal. `logs/dup` and no value keep them
    on spans.
    """
    choice = os.environ.get("OTEL_SEMCONV_EXCEPTION_SIGNAL_OPT_IN", "")
    return choice.strip().lower() != "logs"


def known_methods() -> frozenset[str]:
    """Return the methods recorded as they are, per `OTEL_INSTRUMENTATION_HTTP_KNOWN_METHODS`.

    The variable lists them comma-separated. Unset, they are the methods of
    the HTTP specification. Any other method is recorded as `_OTHER`.
    """
    listed = os.environ.get("OTEL_INSTRUMENTATION_HTTP_KNOWN_METHODS", "")
    methods = frozenset(m.strip() for m in listed.split(",") if m.strip())
    return methods or _HTTP_METHODS


def excluding(
    framework: str,
    app_exclude: Callable[[Scope], bool] | None = None,
) -> Callable[[Scope], bool] | None:
    """Return `app_exclude`, also skipping URLs the environment names.

    `OTEL_PYTHON_{FRAMEWORK}_EXCLUDED_URLS`, or `OTEL_PYTHON_EXCLUDED_URLS`
    when it is unset, holds comma-separated regular expressions. A request
    whose full URL without its query one of them matches is neither traced
    nor measured.
    """
    listed = os.environ.get(
        f"OTEL_PYTHON_{framework.upper()}_EXCLUDED_URLS",
        os.environ.get("OTEL_PYTHON_EXCLUDED_URLS", ""),
    )
    patterns = [entry.strip() for entry in listed.split(",") if entry.strip()]
    if not patterns:
        return app_exclude
    excluded = re.compile("|".join(patterns))

    def exclude(scope: Scope) -> bool:
        if app_exclude is not None and app_exclude(scope):
            return True
        return excluded.search(_url_of(scope)) is not None

    return exclude


def _host(scope: Scope) -> str | None:
    """Return the request's `Host` header, `None` without one."""
    return next(
        (
            value.decode("latin-1")
            for name, value in scope.get("headers", ())
            if name == b"host"
        ),
        None,
    )


def _url_of(scope: Scope) -> str:
    """Return the request's full URL without its query, as it was sent."""
    host = _host(scope)
    if host is None:
        address, port = scope.get("server") or _NO_SERVER
        host = address if port == _HTTP_PORT else f"{address}:{port}"
    return f"{scope.get('scheme', 'http')}://{host}{scope.get('path', '')}"


_NO_SERVER: Final = ("0.0.0.0", 80)  # noqa: S104
"""The server address a request without a `Host` header or a server reads as."""

_HTTP_PORT: Final = 80
"""The port a URL leaves out."""


def record_unhandled(scope: Scope, exc: BaseException) -> None:
    """Record an exception the app raised and answered with a crash.

    The request span gets the `exception` event, and `error.type` names
    the exception. Does nothing for a request not being recorded.
    """
    telemetry = scope.get(_KEY)
    if isinstance(telemetry, _Request):
        telemetry.raised(exc)


def _exception_type(exc: BaseException) -> str:
    """Return the `error.type` of an exception: its qualified class name."""
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def normal_close(exc: BaseException) -> bool:
    """Return whether `exc` is a WebSocket the client closed as it should."""
    return (
        type(exc).__name__ == "WebSocketDisconnect"
        and getattr(exc, "code", None) in _NORMAL_CLOSE
    )


def _unconfigured(provider: object) -> bool:
    """Return whether `provider` is OpenTelemetry's stand-in or a no-op."""
    cls = type(provider)
    return (cls.__module__, cls.__name__) in _DEFERRED_PROVIDERS or isinstance(
        provider, (trace.NoOpTracerProvider, metrics.NoOpMeterProvider)
    )


def _server_attributes(scope: Scope) -> dict[str, Any]:
    """Return `server.address` and `server.port`, from `Host` or the server."""
    authority = _host(scope)
    if authority is not None:
        try:
            url = urlsplit("//" + authority)
            address, port = url.hostname, url.port
        except ValueError:
            return {}
        if url.username is not None or url.path or url.query or url.fragment:
            return {}
        if port is None:
            port = _DEFAULT_PORTS.get(scope.get("scheme", "http"))
    else:
        address, port = scope.get("server") or (None, None)
    if address is None:
        return {}
    attributes: dict[str, Any] = {"server.address": address}
    if port is not None:
        attributes["server.port"] = port
    return attributes


def _span_attributes(
    scope: Scope, attributes: dict[str, Any], *, original_method: str
) -> dict[str, Any]:
    """Return the attributes a request span starts with.

    The ones every request records, the server, and for HTTP the path, the
    redacted query and a method recorded as `_OTHER` as it was sent.
    """
    started = {**attributes, **_server_attributes(scope)}
    if scope["type"] == "websocket":
        return started
    started["url.path"] = scope["path"]
    if scope.get("query_string"):
        started["url.query"] = _query(scope["query_string"])
    if attributes["http.request.method"] != original_method:
        started["http.request.method_original"] = original_method
    return started


def _error_type(failure: BaseException | None, status_code: int | None) -> str:
    """Return the `error.type` of a failed request.

    The exception that failed it, else its status, else `incomplete_response`.
    """
    if failure is not None:
        return _exception_type(failure)
    return str(status_code or "incomplete_response")


_SERVER_ERROR: Final = 500
"""The first status that fails a request."""


def _query(query_string: bytes) -> str:
    """Return the query with every sensitive value redacted."""
    return urlencode(
        [
            (key, "REDACTED" if key in _SENSITIVE_QUERY_PARAMETERS else value)
            for key, value in parse_qsl(
                query_string.decode("latin-1"), keep_blank_values=True
            )
        ]
    )


class _HeadersGetter(Getter["Scope"]):
    """Read the propagation headers of an ASGI scope."""

    def get(self, carrier: Scope, key: str) -> list[str] | None:
        lowered = key.lower().encode("latin-1")
        values = [
            value.decode("latin-1")
            for name, value in carrier.get("headers", ())
            if name.lower() == lowered
        ]
        if not values:
            return None
        if key.lower() == "baggage":
            # The baggage propagator reads only the first value.
            return [",".join(values)]
        return values

    def keys(self, carrier: Scope) -> list[str]:
        return [
            name.decode("latin-1") for name, _ in carrier.get("headers", ())
        ]


_HEADERS_GETTER: Final = _HeadersGetter()
"""Reads the propagation headers of every request."""


class _Request:
    """What one request being recorded carries until it finishes.

    With `events` off, an exception is kept but not put on the span.
    """

    __slots__ = ("events", "exceptions", "span")

    def __init__(self, span: Span | None, *, events: bool) -> None:
        self.span = span
        self.events = events
        self.exceptions: list[BaseException] = []

    def raised(self, exc: BaseException) -> None:
        """Keep `exc`, and put it on the span, once."""
        if any(seen is exc for seen in self.exceptions):
            return
        self.exceptions.append(exc)
        if self.events and self.span is not None and self.span.is_recording():
            self.span.record_exception(exc)


class RaisedExceptions:
    """Record an exception the app raises on the request span.

    Runs inside the framework's server error handler, before it answers
    `500`. A WebSocket closed normally records nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            if not normal_close(exc):
                record_unhandled(scope, exc)
            raise


class RequestTelemetry:
    """Record the request span and the HTTP server metrics of every request.

    Runs outside every other layer of the app. A request `exclude` names,
    or one an outer layer already records, is passed through. `unwrap`
    returns the exception the app raised from the one the framework let
    out. `methods` are recorded as they are, and exceptions go on the span
    when `events` is on.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        route: RouteOf,
        tracing: bool,
        exclude: Callable[[Scope], bool] | None,
        methods: frozenset[str],
        events: bool,
        unwrap: Callable[[BaseException], BaseException] | None = None,
    ) -> None:
        self.app = app
        self._route = route
        self._unwrap = unwrap
        self._tracing = tracing
        self._exclude = exclude
        self._known_methods = methods
        self._events = events
        self._tracer_provider: object = None
        self._tracer: Tracer | None = None
        self._meter_provider: object = None
        self._instruments: tuple[Histogram, UpDownCounter] | None = None

    def _tracer_of(self, provider: Any) -> Tracer:  # noqa: ANN401
        if provider is not self._tracer_provider or self._tracer is None:
            self._tracer = provider.get_tracer(
                SCOPE_NAME, _VERSION, schema_url=_SCHEMA_URL
            )
            self._tracer_provider = provider
        return self._tracer

    def _instruments_of(
        self,
        provider: Any,  # noqa: ANN401
    ) -> tuple[Histogram, UpDownCounter]:
        if provider is not self._meter_provider or self._instruments is None:
            meter = provider.get_meter(
                SCOPE_NAME, _VERSION, schema_url=_SCHEMA_URL
            )
            self._instruments = (
                meter.create_histogram(
                    "http.server.request.duration",
                    unit="s",
                    description="Duration of HTTP server requests.",
                    explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
                ),
                meter.create_up_down_counter(
                    "http.server.active_requests",
                    unit="{request}",
                    description="Number of active HTTP server requests.",
                ),
            )
            self._meter_provider = provider
        return self._instruments

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        kind = scope["type"]
        if (
            kind not in ("http", "websocket")
            or _KEY in scope
            or TELEMETRY_KEY in scope
        ):
            await self.app(scope, receive, send)
            return
        # Marked whatever happens next, so a mounted FastAPI app or a
        # nested grelmicro layer records nothing of its own.
        scope[TELEMETRY_KEY] = None
        scope[_KEY] = None
        try:
            if self._exclude is not None and self._exclude(scope):
                await self.app(scope, receive, send)
                return
            await self._record(scope, receive, send)
        finally:
            scope.pop(TELEMETRY_KEY, None)
            scope.pop(_KEY, None)

    async def _record(  # noqa: C901, PLR0912, PLR0915
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        is_websocket = scope["type"] == "websocket"
        tracer_provider = trace.get_tracer_provider()
        tracing = self._tracing and not _unconfigured(tracer_provider)
        meter_provider = metrics.get_meter_provider()
        metering = not is_websocket and not _unconfigured(meter_provider)
        if not tracing and not metering:
            await self.app(scope, receive, send)
            return
        root_path = scope.get("root_path", "")
        path = scope.get("path", "")
        original_method = scope.get("method", "")
        method = (
            original_method
            if original_method in self._known_methods
            else "_OTHER"
        )
        span_method = (
            "WS" if is_websocket else "HTTP" if method == "_OTHER" else method
        )
        attributes: dict[str, Any] = {
            "url.scheme": scope.get("scheme", "ws" if is_websocket else "http"),
        }
        if is_websocket:
            attributes["network.protocol.name"] = "websocket"
        else:
            attributes["http.request.method"] = method
            if scope.get("http_version"):
                attributes["network.protocol.version"] = scope["http_version"]
        active_attributes = {
            k: v
            for k, v in attributes.items()
            if k != "network.protocol.version"
        }
        duration, active = (
            self._instruments_of(meter_provider) if metering else (None, None)
        )
        if active is not None:
            active.add(1, active_attributes)
        started = perf_counter()
        span = None
        parent_token = None
        if tracing:
            parent = propagate.extract(scope, getter=_HEADERS_GETTER)
            parent_token = otel_context.attach(parent)
            span = self._tracer_of(tracer_provider).start_span(
                span_method,
                context=parent,
                kind=SpanKind.SERVER,
                attributes=_span_attributes(
                    scope, attributes, original_method=original_method
                ),
            )
        request = _Request(span, events=self._events)
        scope[_KEY] = request
        status_code: int | None = None
        finished = False
        trailers = False
        disconnected = False

        def finish(error: BaseException | None = None) -> None:  # noqa: C901
            nonlocal finished
            if finished:
                return
            finished = True
            try:
                route = self._route(
                    scope, Answered(root_path, path, status_code)
                )
            except Exception:
                _logger.warning(
                    "Could not read the route of %s", path, exc_info=True
                )
                route = None
            if route is not None:
                attributes["http.route"] = route
            if status_code is not None:
                attributes["http.response.status_code"] = status_code
            failed = (
                error is not None
                or bool(request.exceptions)
                or (
                    not is_websocket
                    and (status_code is None or status_code >= _SERVER_ERROR)
                )
            )
            if failed:
                attributes["error.type"] = _error_type(
                    error
                    or (request.exceptions[-1] if request.exceptions else None),
                    status_code,
                )
            if span is not None:
                if route is not None:
                    span.update_name(f"{span_method} {route}")
                span.set_attributes(attributes)
                if failed:
                    span.set_status(StatusCode.ERROR)
            # Record while the span is current, so exemplars can correlate it.
            if duration is not None:
                duration.record(max(0, perf_counter() - started), attributes)
            if active is not None:
                active.add(-1, active_attributes)
            if span is not None:
                span.end()

        async def wrapped_send(message: Message) -> None:
            nonlocal status_code, trailers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                trailers = message.get("trailers", False)
            await send(message)
            if (
                (
                    message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                    and not trailers
                )
                or (
                    message["type"] == "http.response.trailers"
                    and not message.get("more_trailers", False)
                )
                or message["type"] == "http.response.pathsend"
            ):
                finish()

        async def wrapped_receive() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        current = (
            trace.use_span(
                span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            )
            if span is not None
            else nullcontext()
        )
        try:
            with current:
                try:
                    await self.app(
                        scope,
                        receive if is_websocket else wrapped_receive,
                        send if is_websocket else wrapped_send,
                    )
                except BaseException as exc:
                    failure = exc if self._unwrap is None else self._unwrap(exc)
                    if is_websocket and normal_close(failure):
                        finish()
                    else:
                        if isinstance(failure, Exception) and not finished:
                            request.raised(failure)
                        finish(failure)
                    raise
                finally:
                    if not finished:
                        finish(
                            None
                            if is_websocket
                            else ConnectionError("Client disconnected")
                            if disconnected
                            else RuntimeError("Incomplete ASGI response")
                        )
        finally:
            request.exceptions.clear()
            if parent_token is not None:
                otel_context.detach(parent_token)
