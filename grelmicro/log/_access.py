"""Access log: one structured record per HTTP request, written by grelmicro.

`grelmicro.log.uvicorn` reformats the record uvicorn writes, and that record
carries what uvicorn put in it: the socket peer, the request line, and the
status. This one carries what the app knows. The caller behind the proxy, the
route template rather than the path that matched it, how long it took, and the
trace context, so the line and the span it belongs to say the same words.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, Self

from typing_extensions import Doc

from grelmicro._paths import _PREFIX, matches, route_path, selects
from grelmicro._redact import _redact_query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping
    from types import TracebackType

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["AccessLog", "AccessLogMiddleware"]

logger = logging.getLogger("grelmicro.access")
"""Where the access record is written. Silence it to drop every one."""

_UVICORN_ACCESS_LOGGER: Final = "uvicorn.access"
"""Uvicorn's own access logger, silenced while this one is registered."""

DEFAULT_QUIET: Final = ("/livez", "/readyz", "/healthz", "/metrics")
"""Paths logged at debug while they answer.

Kubernetes polls the probes every few seconds for the life of the pod, and a
scrape arrives as often, so at info they crowd out every request a person
wanted to read. A probe that fails is logged like any other failure, because
a refused readiness check is often the only line saying the kubelet asked.
"""

_SERVER_ERROR: Final = 500
_CLIENT_ERROR: Final = 400


class _Silence(logging.Filter):
    """Drop every record. Added to uvicorn's access logger, and only there."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: ARG002
        """Refuse the record."""
        return False


class AccessLogMiddleware:
    """Write one structured record per request.

    Pure ASGI, so it runs on FastAPI, Starlette, Litestar, and anything else
    that speaks ASGI. `micro.install(app)` adds it for a registered
    `AccessLog`, and an app on a framework `install` does not know wraps
    itself with it.

    The record carries [OpenTelemetry semantic
    conventions](https://opentelemetry.io/docs/specs/semconv/http/http-spans/)
    field names, the same ones the request span carries, so a backend reads
    one vocabulary across the log and the trace.
    """

    def __init__(
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        include: Annotated[
            tuple[str, ...],
            Doc(
                "Paths to log. Empty means every path. A pattern ending in "
                "`*` matches as a prefix."
            ),
        ] = (),
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths to leave alone, whatever `include` says. Nothing is "
                "written for them, at any level."
            ),
        ] = (),
        quiet: Annotated[
            tuple[str, ...],
            Doc(
                "Paths logged at debug while they answer, and at the level "
                "their status earns when they do not."
            ),
        ] = DEFAULT_QUIET,
        query: Annotated[
            bool,
            Doc("Whether the record carries `url.query`, redacted."),
        ] = True,
        user_agent: Annotated[
            bool,
            Doc("Whether the record carries `user_agent.original`."),
        ] = True,
    ) -> None:
        """Initialize the middleware with what to log and what to leave out."""
        self.app = app
        self.include = include
        self.exclude = exclude
        self.quiet = quiet
        self.query = query
        self.user_agent = user_agent
        # Decided once, because the answers are the same for every request
        # and this runs on the request path. Nothing named means nothing to
        # match, and a plain path is a set lookup rather than a walk
        # through the patterns.
        self._filtering = bool(include or exclude)
        self._quiet_paths = frozenset(
            path for path in quiet if not path.endswith(_PREFIX)
        )
        self._quiet_patterns = tuple(
            path for path in quiet if path.endswith(_PREFIX)
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Time the request, then write what it did."""
        if scope["type"] != "http" or (
            self._filtering
            and not selects(
                route_path(scope),
                include=self.include,
                exclude=self.exclude,
            )
        ):
            await self.app(scope, receive, send)
            return
        status = _SERVER_ERROR
        error: BaseException | None = None
        started = time.perf_counter()

        async def _send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except BaseException as exc:
            # An exception on the way out is the answer the client got, so
            # it belongs on the record. The framework still owns what it
            # renders, so it is re-raised untouched.
            error = exc
            raise
        finally:
            self._write(
                scope,
                status=status,
                error=error,
                duration=time.perf_counter() - started,
            )

    def _write(
        self,
        scope: Scope,
        *,
        status: int,
        error: BaseException | None,
        duration: float,
    ) -> None:
        """Write the record for one finished request."""
        path = route_path(scope)
        level = _level_of(status, quiet=self._is_quiet(path))
        if not logger.isEnabledFor(level):
            return
        method = scope.get("method", "")
        # What the caller asked for, prefix and all, which is what an
        # access log is read for. The prefix comes off for matching alone,
        # so a pattern is the same wherever the app is mounted.
        asked = scope.get("path", path)
        fields: dict[str, Any] = {
            "http.request.method": method,
            "url.path": asked,
            "url.scheme": scope.get("scheme", "http"),
            "http.response.status_code": status,
            "http.server.request.duration": round(duration, 6),
        }
        route = _route_template(scope)
        if route is not None:
            fields["http.route"] = route
        client = _client_address(scope)
        if client is not None:
            fields["client.address"] = client
        version = scope.get("http_version")
        if version:
            fields["network.protocol.version"] = version
        if self.query:
            query = _query(scope)
            if query is not None:
                fields["url.query"] = query
        if self.user_agent:
            agent = _header(scope, b"user-agent")
            if agent is not None:
                fields["user_agent.original"] = agent
        if error is not None:
            fields["error.type"] = type(error).__qualname__
        logger.log(level, "%s %s %s", method, asked, status, extra=fields)

    def _is_quiet(self, path: str) -> bool:
        """Return whether this path only speaks up when it fails."""
        return path in self._quiet_paths or (
            bool(self._quiet_patterns) and matches(path, self._quiet_patterns)
        )


class AccessLog:
    """Write one structured record per HTTP request.

    Register it and `micro.install(app)` adds the middleware:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.log import AccessLog, Log

    micro = Grelmicro(uses=[Log(), AccessLog()])
    micro.install(app)
    ```

    While it is registered, uvicorn's own access log is silenced, because
    two access logs on one stream is worse than either alone. Nothing else
    about uvicorn's logging changes.

    The level follows the answer: `5xx` is an error, `4xx` a warning, and
    anything else information. The probe paths and `/metrics` are logged at
    debug while they answer, so they stay out of the way without going
    missing when they fail.

    Read more in the [Access Log](../logging/access.md) docs.
    """

    kind: ClassVar[str] = "access_log"
    singleton: ClassVar[bool] = True
    singleton_reason: ClassVar[str] = (
        "One access log answers for the whole app, so two would write every "
        "request twice"
    )

    def __init__(
        self,
        *,
        include: Annotated[
            tuple[str, ...],
            Doc(
                "Paths to log. Empty (the default) means every path. A "
                "pattern ending in `*` matches as a prefix."
            ),
        ] = (),
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths to leave alone, whatever `include` says. Nothing is "
                "written for them, at any level."
            ),
        ] = (),
        quiet: Annotated[
            tuple[str, ...],
            Doc(
                "Paths logged at debug while they answer, and at the level "
                "their status earns when they do not. Defaults to the probe "
                "paths and `/metrics`. Pass `()` to log them like anything "
                "else."
            ),
        ] = DEFAULT_QUIET,
        query: Annotated[
            bool,
            Doc(
                "Whether the record carries `url.query`. Redacted through "
                "the same rules the rest of the library redacts a URL with, "
                "so a token in a query string never reaches the sink."
            ),
        ] = True,
        user_agent: Annotated[
            bool,
            Doc("Whether the record carries `user_agent.original`."),
        ] = True,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
    ) -> None:
        """Initialize the component with what to log and what to leave out."""
        self._name = name
        self._options: dict[str, Any] = {
            "include": include,
            "exclude": exclude,
            "quiet": quiet,
            "query": query,
            "user_agent": user_agent,
        }
        self._silence = _Silence()

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with."""
        return AccessLogMiddleware, dict(self._options)

    async def __aenter__(self) -> Self:
        """Silence uvicorn's access log for as long as this one is open."""
        logging.getLogger(_UVICORN_ACCESS_LOGGER).addFilter(self._silence)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Give uvicorn its access log back."""
        logging.getLogger(_UVICORN_ACCESS_LOGGER).removeFilter(self._silence)


def _level_of(status: int, *, quiet: bool) -> int:
    """Return the level a finished request is written at.

    The answer decides it: a `5xx` is the service failing, a `4xx` is the
    caller being turned away, and anything else is what the service is for.
    A quiet path that answered says nothing at info.
    """
    if status >= _SERVER_ERROR:
        return logging.ERROR
    if status >= _CLIENT_ERROR:
        return logging.WARNING
    return logging.DEBUG if quiet else logging.INFO


def _route_template(scope: Scope) -> str | None:
    """Return the route template the request matched, when there is one.

    Read after the app has answered, because that is when the router has
    written what it matched into the scope. There is no standard key for
    it, so each framework is read the way it records it: Litestar writes
    `path_template`, and FastAPI a route carrying `path_format`. Starlette
    records neither, so a plain Starlette app leaves the field out rather
    than guessing a template from the values that filled it.

    The mount or proxy prefix goes back on, so the route reads as the path
    it grouped, which is what `url.path` on the same record carries.
    """
    template = scope.get("path_template")
    if not isinstance(template, str):
        route = scope.get("route")
        template = getattr(route, "path_format", None) or getattr(
            route, "path", None
        )
    if not isinstance(template, str):
        return None
    root = scope.get("root_path", "").rstrip("/")
    return f"{root}{template}"


def _client_address(scope: Scope) -> str | None:
    """Return the caller's address, resolved rather than assumed.

    `ClientAddressMiddleware` resolves the caller behind a proxy and caches
    it on the request, and that is the address an access log is read for.
    Without it the transport peer is all there is, which behind an ingress
    is the ingress.
    """
    resolved = (scope.get("state") or {}).get("client_address")
    address = getattr(resolved, "ip", None)
    if address is not None:
        return str(address)
    client = scope.get("client")
    return str(client[0]) if client else None


def _query(scope: Scope) -> str | None:
    """Return the query string, redacted, or `None` when there is none."""
    raw = scope.get("query_string") or b""
    if not raw:
        return None
    return _redact_query(raw.decode("latin-1"))


def _header(scope: Scope, name: bytes) -> str | None:
    """Return one request header, or `None` when it was not sent."""
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1", "replace")
    return None
