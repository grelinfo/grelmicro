"""The operational endpoints, on a port of their own.

A worker, a consumer, or a scheduler runs no web framework, and Kubernetes
still asks it whether it is alive. `OpsServer` answers on its own port, over
a small HTTP/1.1 server built from the standard library. It adds no
dependency, and it serves nothing but the endpoints grelmicro renders.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from email.utils import formatdate
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    NamedTuple,
    Self,
)
from urllib.parse import unquote

from pydantic import (
    BaseModel,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
)
from typing_extensions import Doc

from grelmicro._config import default_env_prefix, resolve_config
from grelmicro._endpoints import (
    HTTP_OK,
    HTTP_SERVICE_UNAVAILABLE,
    Rendered,
    build_asgi,
    normalize,
    response_headers,
)
from grelmicro.health._endpoints import health_routes
from grelmicro.http._kinds import phrase_of
from grelmicro.http.errors import OpsServerError
from grelmicro.metrics._endpoints import metrics_routes
from grelmicro.metrics.config import MetricsExporterType

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from types import TracebackType

    from grelmicro._app import Grelmicro
    from grelmicro._endpoints import ASGIApp, Handler, Message

    Scope = MutableMapping[str, Any]

__all__ = ["OpsServer", "OpsServerConfig"]

logger = getLogger("grelmicro.http")

_HTTP_BAD_REQUEST: Final = 400
_HTTP_REQUEST_TIMEOUT: Final = 408
_HTTP_CONTENT_TOO_LARGE: Final = 413
_HTTP_HEADERS_TOO_LARGE: Final = 431
_HTTP_INTERNAL_SERVER_ERROR: Final = 500
_HTTP_NOT_IMPLEMENTED: Final = 501

_HEADER_LIMIT: Final = 8192
"""Bytes one request line or header may take before it is refused."""

_MAX_HEADERS: Final = 64
"""Headers a request may carry. A probe sends a handful."""

_MAX_BODY: Final = 8192
"""Body bytes read and dropped, so the connection closes cleanly."""

_REQUEST_LINE_PARTS: Final = 3

_LINGER_SECONDS: Final = 0.5
"""How long a refused caller is read before the connection closes."""

_LIVEZ: Final = "/livez"
"""The one path answered while the app is still opening."""


class _Request(NamedTuple):
    """What the server needs from a request line and its headers."""

    method: str
    target: str
    headers: list[tuple[bytes, bytes]]


@dataclass(slots=True)
class _Answer:
    """What a connection still owes its caller.

    `started` says a response is already on the wire, and nothing more may
    be written on that connection. `status` is a status-only answer that
    has not been written yet.
    """

    writer: asyncio.StreamWriter
    started: bool = False
    status: int | None = None


class _RefuseRequest(Exception):  # noqa: N818
    """Raised to answer with a status code and nothing else."""

    def __init__(self, status: int) -> None:
        """Initialize with the status to answer."""
        self.status = status


class OpsServerConfig(BaseModel, frozen=True, extra="forbid"):
    """Ops Server Config."""

    host: Annotated[
        str,
        Doc(
            "Address to bind. Empty binds every interface, IPv4 and IPv6, "
            "which is what the kubelet needs to reach the pod. Set "
            "``127.0.0.1`` to keep the port on loopback."
        ),
    ] = ""
    port: Annotated[
        int,
        Field(ge=1, le=65535),
        Doc("Port to listen on."),
    ] = 8080
    show_details: Annotated[
        bool,
        Doc(
            "Whether ``/healthz`` includes each check's verbose ``details`` "
            "field. ``False`` strips them."
        ),
    ] = False
    request_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Seconds one request may take, from the first byte read to the "
            "last byte written. A connection that goes quiet is dropped."
        ),
    ] = 10.0
    shutdown_timeout: Annotated[
        NonNegativeFloat,
        Doc(
            "Seconds in-flight requests get to finish on shutdown before "
            "they are cancelled."
        ),
    ] = 5.0
    max_connections: Annotated[
        PositiveInt,
        Doc(
            "Connections served at once. Beyond it a connection is answered "
            "``503`` and closed."
        ),
    ] = 32


class OpsServer:
    """Serve the health and metrics endpoints on a port of their own.

    For a process that runs no web framework: a FastStream consumer, a
    scheduler, a worker. Kubernetes still needs somewhere to send its
    probes, and Prometheus still needs somewhere to scrape.

    ```python
    from grelmicro import Grelmicro
    from grelmicro.health import HealthChecks
    from grelmicro.http import OpsServer
    from grelmicro.metrics import Metrics

    micro = Grelmicro(
        uses=[
            HealthChecks(auto_health=True),
            Metrics(exporter="prometheus"),
            OpsServer(port=8080),
        ]
    )
    ```

    It serves what the app registers: the three health endpoints when a
    `HealthChecks` is registered, and ``/metrics`` when a `Metrics` is. An
    app that registers neither has nothing to serve, so the server says so
    instead of listening.

    It speaks HTTP/1.1 over the standard library, answers one request per
    connection, and serves nothing but these endpoints. It speaks no TLS
    and reads no request body, so give it the pod network rather than an
    ingress.

    Register it first in `uses=[...]` and it closes last, so the probes keep
    answering while the rest of the app drains.

    Read more in the [Ops Server](../http/server.md) docs.
    """

    kind: ClassVar[str] = "ops"

    def __init__(
        self,
        *,
        port: Annotated[
            int | None,
            Doc(
                """
                Port to listen on.

                Default: 8080. When unset and env reads are enabled (see
                ``env_load`` and ``GREL_ENV_LOAD``), resolves from the
                environment variable ``GREL_OPS_PORT`` (or
                ``GREL_OPS_{NAME_UPPER}_PORT`` for a named instance) if
                present, otherwise falls back to the ``OpsServerConfig``
                default.
                """
            ),
        ] = None,
        host: Annotated[
            str | None,
            Doc(
                """
                Address to bind. Empty binds every interface, IPv4 and
                IPv6.

                Default: empty. Resolves from ``GREL_OPS_HOST`` the way
                ``port`` does.
                """
            ),
        ] = None,
        show_details: Annotated[
            bool | None,
            Doc(
                """
                Whether ``/healthz`` includes each check's verbose
                ``details`` field.

                Default: False. Resolves from ``GREL_OPS_SHOW_DETAILS`` the
                way ``port`` does.
                """
            ),
        ] = None,
        request_timeout: Annotated[
            float | None,
            Doc(
                """
                Seconds one request may take, from the first byte read to
                the last byte written.

                Default: 10.0. Resolves from ``GREL_OPS_REQUEST_TIMEOUT``
                the way ``port`` does.
                """
            ),
        ] = None,
        shutdown_timeout: Annotated[
            float | None,
            Doc(
                """
                Seconds in-flight requests get to finish on shutdown.

                Default: 5.0. Resolves from ``GREL_OPS_SHUTDOWN_TIMEOUT``
                the way ``port`` does.
                """
            ),
        ] = None,
        max_connections: Annotated[
            int | None,
            Doc(
                """
                Connections served at once.

                Default: 32. Resolves from ``GREL_OPS_MAX_CONNECTIONS`` the
                way ``port`` does.
                """
            ),
        ] = None,
        name: Annotated[
            str,
            Doc(
                "Registration name. Two `OpsServer` instances may coexist "
                "on one `Grelmicro` under different names, on different "
                "ports."
            ),
        ] = "default",
        env_prefix: Annotated[
            str | None,
            Doc(
                """
                Override the auto-derived environment variable prefix.

                Default: ``GREL_OPS_`` for the default instance,
                ``GREL_OPS_{NAME_UPPER}_`` for a named one.
                """
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                """
                Whether to read environment variables.

                When None (the default), follow the process-wide
                ``GREL_ENV_LOAD`` flag. Pass True or False to override the
                flag for this construction.
                """
            ),
        ] = None,
    ) -> None:
        """Initialize the ops server."""
        config = resolve_config(
            OpsServerConfig,
            explicit=None,
            kwargs={
                "host": host,
                "port": port,
                "show_details": show_details,
                "request_timeout": request_timeout,
                "shutdown_timeout": shutdown_timeout,
                "max_connections": max_connections,
            },
            env_prefix=env_prefix or default_env_prefix("OPS", name),
            env_load=env_load,
        )
        self._setup(config, name=name)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            OpsServerConfig,
            Doc(
                """
                The pre-built ops server configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault, or a
                ``pydantic-settings`` aggregator). The environment path is
                bypassed and the config is used as-is.
                """
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc("Registration name. Defaults to `'default'`."),
        ] = "default",
    ) -> Self:
        """Construct an `OpsServer` from a pre-built `OpsServerConfig`."""
        instance = cls.__new__(cls)
        instance._setup(config, name=name)  # noqa: SLF001
        return instance

    def _setup(self, config: OpsServerConfig, *, name: str) -> None:
        """Wire the resolved configuration onto the instance."""
        self._config = config
        self._name = name
        self._micro: Grelmicro | None = None
        self._app: ASGIApp | None = None
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._paths: tuple[str, ...] = ()
        self._active = 0

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def config(self) -> OpsServerConfig:
        """Return the resolved configuration."""
        return self._config

    @property
    def paths(self) -> tuple[str, ...]:
        """Return the paths served.

        Empty until the app it belongs to has finished opening, because
        until then it answers `/livez` alone and what it will serve is not
        settled: a `Metrics` still opening has no exporter to read.
        """
        self._build()
        return self._paths

    async def __aenter__(self) -> Self:
        """Bind the port and start serving.

        Raises:
            OpsServerError: If no `Grelmicro` app is active, the app
                registers nothing to serve, or the port cannot be bound.
        """
        from grelmicro._app import Grelmicro  # noqa: PLC0415

        try:
            micro = Grelmicro.current()
        except LookupError as exc:
            msg = (
                "OpsServer serves what a Grelmicro app registers, so it has "
                "to be registered on one: Grelmicro(uses=[..., OpsServer()])."
            )
            raise OpsServerError(msg) from exc
        self._check_has_something_to_serve(micro)
        self._micro = micro
        try:
            self._server = await asyncio.start_server(
                self._handle,
                self._config.host or None,
                self._config.port,
                limit=_HEADER_LIMIT,
            )
        except OSError as exc:
            self._micro = None
            msg = (
                f"OpsServer cannot listen on "
                f"{self._config.host or '*'}:{self._config.port}: {exc}."
            )
            raise OpsServerError(msg) from exc
        logger.info(
            "ops server listening on %s:%s",
            self._config.host or "*",
            self._config.port,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop accepting, let in-flight requests finish, then close."""
        if self._server is not None:
            # Closed, then drained, then awaited: `wait_closed` waits for
            # the handlers too, so awaiting it first would hold the app
            # open for as long as a request takes and leave
            # `shutdown_timeout` with nothing to bound.
            self._server.close()
            await self._drain()
            await self._server.wait_closed()
            self._server = None
        else:  # pragma: no cover
            # Unreachable through the app, which never closes what it did
            # not open. Kept so a direct `__aexit__` still drains.
            await self._drain()
        self._micro = None
        self._app = None
        self._paths = ()

    async def _drain(self) -> None:
        """Let in-flight requests finish, cancelling stragglers on timeout.

        The live set is read again after the wait, not the snapshot taken
        before it: a connection accepted in the same loop iteration as
        `close()` adds itself while the wait is running, and cancelling
        the snapshot alone would leave it to run out its own timeout.
        """
        if self._connections and self._config.shutdown_timeout > 0:
            await asyncio.wait(
                list(self._connections),
                timeout=self._config.shutdown_timeout,
            )
        pending = list(self._connections)
        for handle in pending:
            handle.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _check_has_something_to_serve(self, micro: Grelmicro) -> None:
        """Refuse an app that registers no endpoint, before binding a port.

        Registration happens before anything is opened, so this is knowable
        at startup even though what each component will serve is not.

        Raises:
            OpsServerError: If the app registers neither a `HealthChecks`
                nor a `Metrics` under the default name.
        """
        if not _registered_kinds(micro) & {"health", "metrics"}:
            msg = (
                "OpsServer has nothing to serve. Register a HealthChecks, a "
                "Metrics, or both: Grelmicro(uses=[HealthChecks(), "
                "OpsServer()])."
            )
            raise OpsServerError(msg)

    def _build(self) -> ASGIApp | None:
        """Return the app for the endpoints, once there is one to build.

        Built the first time the `Grelmicro` app is fully open, and not
        before: a component registered after this one is still opening, so
        its checks are not all registered and its config is not resolved
        yet. Until then the server answers `/livez` and nothing else, which
        is what a process that is alive but not ready owes an orchestrator.
        """
        if self._app is not None:
            return self._app
        micro = self._micro
        if micro is None or not micro.opened:
            return None
        # Components resolve per request rather than here, so one swapped
        # by `micro.override(...)` answers the probe a test expects.
        kinds = _registered_kinds(micro)
        routes: dict[str, Handler] = {}
        if "health" in kinds:
            routes |= health_routes(show_details=self._config.show_details)
        if "metrics" in kinds:
            routes |= self._metrics_routes(micro)
        self._paths = tuple(routes)
        self._app = build_asgi(routes)
        logger.info(
            "ops server serving %s", ", ".join(self._paths) or "only /livez"
        )
        return self._app

    def _metrics_routes(self, micro: Grelmicro) -> dict[str, Handler]:
        """Return the metrics route, when the component can render one.

        Only the `prometheus` exporter builds a registry to render, so any
        other one leaves `/metrics` unserved instead of answering every
        scrape with a `500`.
        """
        metrics = micro.get("metrics", "default")
        if metrics.config.exporter == MetricsExporterType.PROMETHEUS:
            return metrics_routes()
        logger.warning(
            "ops server serves no /metrics: the Metrics component uses "
            "exporter=%r, and only 'prometheus' renders an exposition",
            metrics.config.exporter,
        )
        return {}

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one connection: one request, one answer, then close."""
        task = asyncio.current_task()
        if task is not None:  # pragma: no branch
            self._connections.add(task)
        self._active += 1
        # Decided on arrival, answered after the request is read. Deciding
        # here keeps it first-come first-served, so a burst sheds the
        # newest caller rather than the probe that has waited longest.
        admitted = self._active <= self._config.max_connections
        answer = _Answer(writer)
        try:
            async with asyncio.timeout(self._config.request_timeout):
                await self._serve(reader, answer, admitted=admitted)
        except _RefuseRequest as refusal:
            answer.status = refusal.status
        except TimeoutError:
            answer.status = _HTTP_REQUEST_TIMEOUT
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ops server failed to answer a request")
            answer.status = _HTTP_INTERNAL_SERVER_ERROR
        finally:
            self._active -= 1
            if task is not None:  # pragma: no branch
                self._connections.discard(task)
            await self._close(reader, writer, answer)

    async def _close(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        answer: _Answer,
    ) -> None:
        """Write whatever is still owed, then close the connection.

        Nothing is written when a response is already on the wire, so a
        deadline that lands mid-write closes the connection rather than
        appending a second status line to a response the client is
        already reading.
        """
        with suppress(OSError, asyncio.CancelledError, TimeoutError):
            if answer.status is not None and not answer.started:
                # The socket may still hold the body this answer refuses,
                # and closing on unread bytes resets the connection,
                # taking the answer with it.
                await _drain_quietly(reader)
                await _write_status(writer, answer.status)
        writer.close()
        with suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        answer: _Answer,
        *,
        admitted: bool,
    ) -> None:
        """Read one request, run the app, and write what it answered.

        Raises:
            _RefuseRequest: If the request cannot be read as one this
                server answers, or the server is over its connection cap.
        """
        request = await _read_request(reader)
        if request is None:
            return
        if not admitted:
            raise _RefuseRequest(HTTP_SERVICE_UNAVAILABLE)
        micro = self._micro
        app = self._build()
        if micro is None:  # pragma: no cover
            # Unreachable: a connection is accepted only between
            # `__aenter__` and `__aexit__`, which is the window `_micro`
            # is set for.
            raise _RefuseRequest(_HTTP_INTERNAL_SERVER_ERROR)
        if app is None:
            # The app is still opening: alive, and not ready for anything
            # that reads a component which may not be there yet.
            answer.status = (
                HTTP_OK
                if _route_of(request) == _LIVEZ
                else HTTP_SERVICE_UNAVAILABLE
            )
            return
        # Bound for this task exactly as `GrelmicroMiddleware` binds it for
        # a framework's request task, so a health check resolves its
        # backend ambiently here too.
        token = micro._bind_current()  # noqa: SLF001
        try:
            status, headers, body = await _call_app(
                app, _scope(request, answer.writer)
            )
        finally:
            micro._reset_current(token)  # noqa: SLF001
        answer.started = True
        await _write(
            answer.writer,
            status,
            headers,
            body,
            head=request.method == "HEAD",
        )


async def _read_request(reader: asyncio.StreamReader) -> _Request | None:
    """Read one request line and its headers, dropping any body.

    Returns `None` when the peer closed without sending a request, which is
    what a TCP check of the port itself does.

    Raises:
        _RefuseRequest: If the request is malformed, too large, or uses a
            framing this server does not implement.
    """
    line = await _read_line(reader)
    if line is None:
        return None
    parts = line.split()
    if len(parts) != _REQUEST_LINE_PARTS:
        raise _RefuseRequest(_HTTP_BAD_REQUEST)
    method, target, _version = parts
    headers: list[tuple[bytes, bytes]] = []
    while True:
        header = await _read_line(reader)
        if header is None:
            raise _RefuseRequest(_HTTP_BAD_REQUEST)
        if not header:
            break
        if len(headers) >= _MAX_HEADERS:
            raise _RefuseRequest(_HTTP_HEADERS_TOO_LARGE)
        name, separator, value = header.partition(b":")
        if not separator:
            raise _RefuseRequest(_HTTP_BAD_REQUEST)
        headers.append((name.strip().lower(), value.strip()))
    await _drop_body(reader, headers)
    return _Request(method.decode("latin-1"), target.decode("latin-1"), headers)


async def _read_line(reader: asyncio.StreamReader) -> bytes | None:
    """Read one line, without its terminator.

    Returns `None` when the peer closed the connection first.

    Raises:
        _RefuseRequest: If the line is longer than the header limit.
    """
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError:
        return None
    except (asyncio.LimitOverrunError, ValueError) as exc:
        raise _RefuseRequest(_HTTP_HEADERS_TOO_LARGE) from exc
    return line.rstrip(b"\r\n")


async def _drop_body(
    reader: asyncio.StreamReader, headers: list[tuple[bytes, bytes]]
) -> None:
    """Read and discard a request body, so the connection closes cleanly.

    No endpoint here reads a body, and leaving one unread turns the close
    into a reset the client reports instead of the answer.

    Raises:
        _RefuseRequest: If the body is chunked, unmeasurable, or too large.
    """
    if any(name == b"transfer-encoding" for name, _ in headers):
        raise _RefuseRequest(_HTTP_NOT_IMPLEMENTED)
    raw = [value for name, value in headers if name == b"content-length"]
    if not raw:
        return
    # Digits only: `int` would also read `-1`, `+5`, ` 5 ` and `1_0`, and
    # a length nothing else on the wire agrees with is how a request is
    # smuggled past whatever reads it differently.
    if not all(value.isdigit() for value in raw):
        raise _RefuseRequest(_HTTP_BAD_REQUEST)
    lengths = {int(value) for value in raw}
    if len(lengths) > 1:
        # Repeated and agreeing is legal (RFC 9112), and only a pair that
        # disagrees is malformed.
        raise _RefuseRequest(_HTTP_BAD_REQUEST)
    length = lengths.pop()
    if length > _MAX_BODY:
        raise _RefuseRequest(_HTTP_CONTENT_TOO_LARGE)
    with suppress(asyncio.IncompleteReadError):
        await reader.readexactly(length)


def _scope(request: _Request, writer: asyncio.StreamWriter) -> Scope:
    """Build the ASGI scope of a request this server read."""
    path, _, query = request.target.partition("?")
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "http",
        "path": unquote(path),
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": request.headers,
        "client": _address(writer, "peername"),
        "server": _address(writer, "sockname"),
        "state": {},
    }


def _address(writer: asyncio.StreamWriter, kind: str) -> tuple[str, int] | None:
    """Return one end of the socket as ASGI spells it, or `None`."""
    info = writer.get_extra_info(kind)
    if not info:  # pragma: no cover
        # A transport may report neither end, and the scope allows `None`.
        return None
    return (info[0], info[1])


async def _call_app(
    app: ASGIApp, scope: Scope
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Run the ASGI app to completion and collect what it sent."""
    status = _HTTP_INTERNAL_SERVER_ERROR
    headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = list(message["headers"])
        else:
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, headers, b"".join(chunks)


def _registered_kinds(micro: Grelmicro) -> set[str]:
    """Return the kinds registered under the default name on the app."""
    return {
        component.kind
        for component in micro.components
        if component.name == "default"
    }


async def _drain_quietly(reader: asyncio.StreamReader) -> None:
    """Read and drop whatever the caller is still sending, briefly.

    Bounded by a short deadline rather than a length, because the caller
    that gets a refusal is the one whose framing was not read.
    """
    with suppress(OSError, TimeoutError, asyncio.IncompleteReadError):
        async with asyncio.timeout(_LINGER_SECONDS):
            while await reader.read(_MAX_BODY):
                pass


def _route_of(request: _Request) -> str:
    """Return the path a request names, in the shape routes are keyed by."""
    path, _, _query = request.target.partition("?")
    return normalize(unquote(path))


async def _write_status(writer: asyncio.StreamWriter, status: int) -> None:
    """Write a status-only answer the server produced itself."""
    rendered = Rendered(status, b"")
    await _write(
        writer, status, response_headers(rendered), rendered.body, head=False
    )


async def _write(
    writer: asyncio.StreamWriter,
    status: int,
    headers: list[tuple[bytes, bytes]],
    body: bytes,
    *,
    head: bool,
) -> None:
    """Write one HTTP/1.1 response, then let the connection close.

    A `HEAD` answer carries the headers a `GET` would, `Content-Length`
    included, and no body, which is what a `HEAD` is for.
    """
    lines = [f"HTTP/1.1 {status} {phrase_of(status)}".encode("latin-1")]
    lines += [b"%s: %s" % (name, value) for name, value in headers]
    lines.append(b"date: " + formatdate(usegmt=True).encode("latin-1"))
    lines.append(b"connection: close")
    writer.write(b"\r\n".join(lines) + b"\r\n\r\n")
    if not head and body:
        writer.write(body)
    await writer.drain()
