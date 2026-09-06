"""Access log: one structured record per HTTP request, written by grelmicro.

`grelmicro.log.uvicorn` reformats the record uvicorn writes, and that record
carries what uvicorn put in it: the socket peer, the request line, and the
status. This one carries what the app knows. The caller behind the proxy, the
route template rather than the path that matched it, how long it took, and the
trace context, so the line and the span it belongs to say the same words.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, Self

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._config import (
    Live,
    Reconfigurable,
    build_config,
    env_prefixes,
    resolve_config,
)
from grelmicro._paths import (
    _PREFIX,
    PathPatterns,
    as_patterns,
    matches,
    route_path,
    selects,
)
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


class AccessLogConfig(BaseModel, frozen=True, extra="forbid"):
    """Access Log Config."""

    include: Annotated[
        PathPatterns,
        Doc(
            "Paths to log. Empty means every path. A pattern ending in `*` "
            "matches as a prefix."
        ),
    ] = ()
    exclude: Annotated[
        PathPatterns,
        Doc(
            "Paths to leave alone, whatever `include` says. Nothing is "
            "written for them, at any level."
        ),
    ] = ()
    quiet: Annotated[
        PathPatterns,
        Doc(
            "Paths logged at debug while they answer, and at the level "
            "their status earns when they do not."
        ),
    ] = DEFAULT_QUIET
    query: Annotated[
        bool,
        Doc("Whether the record carries `url.query`, redacted."),
    ] = True
    user_agent: Annotated[
        bool,
        Doc("Whether the record carries `user_agent.original`."),
    ] = True


@dataclass(frozen=True, slots=True)
class _State:
    """What the middleware answers one request from.

    Holds the configuration beside the values derived from it, so a
    reader takes both in one read and can never pair a new pattern set
    with the lookup table built for the old one.
    """

    config: AccessLogConfig
    filtering: bool
    quiet_paths: frozenset[str]
    quiet_patterns: tuple[str, ...]


def _state_of(config: AccessLogConfig) -> _State:
    """Derive what the request path needs from a configuration.

    Decided once per configuration, because the answers are the same for
    every request. Nothing named means nothing to match, and a plain path
    is a set lookup rather than a walk through the patterns.
    """
    return _State(
        config=config,
        filtering=bool(config.include or config.exclude),
        quiet_paths=frozenset(
            path for path in config.quiet if not path.endswith(_PREFIX)
        ),
        quiet_patterns=tuple(
            path for path in config.quiet if path.endswith(_PREFIX)
        ),
    )


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
        live: Annotated[
            Live[_State] | None,
            Doc(
                "The cell a registered `AccessLog` publishes its snapshot "
                "into, filled by `micro.install(app)`. Passing it makes "
                "the other options the component's to decide."
            ),
        ] = None,
    ) -> None:
        """Initialize the middleware with what to log and what to leave out."""
        self.app = app
        # A middleware built by hand owns its cell and never sees a new
        # snapshot, so the two doors read exactly the same way and the
        # request path has one shape rather than a branch.
        self._live = (
            live
            if live is not None
            else Live(
                _state_of(
                    build_config(
                        AccessLogConfig,
                        include=as_patterns(include, name="include"),
                        exclude=as_patterns(exclude, name="exclude"),
                        quiet=as_patterns(quiet, name="quiet"),
                        query=query,
                        user_agent=user_agent,
                    )
                )
            )
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Time the request, then write what it did."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # One read, at the top, for the whole request. A reconfigure
        # publishes a new snapshot between requests, and one already
        # running finishes on the one it started with.
        state = self._live.state
        # Read before the app runs, and kept. A router rewrites `path` in
        # place on some frameworks and a mount rewrites `root_path`, so
        # reading either afterwards answers for a different request than
        # the one that arrived, and the two matches would disagree.
        asked = scope.get("path", "")
        route = route_path(scope)
        if state.filtering and not selects(
            route,
            include=state.config.include,
            exclude=state.config.exclude,
        ):
            await self.app(scope, receive, send)
            return
        status: int | None = None
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
                state=state,
                asked=asked,
                route=route,
                status=status,
                error=error,
                duration=time.perf_counter() - started,
            )

    def _write(
        self,
        scope: Scope,
        *,
        state: _State,
        asked: str,
        route: str,
        status: int | None,
        error: BaseException | None,
        duration: float,
    ) -> None:
        """Write the record for one finished request.

        A request that raised before any response started is recorded as
        the `500` the framework will send, because that is what the caller
        receives. A cancelled one is recorded with no status at all: the
        caller hung up or the process is stopping, and nothing was sent.
        """
        if (
            status is None
            and error is not None
            and not isinstance(error, asyncio.CancelledError)
        ):
            status = _SERVER_ERROR
        level = _level_of(status, error=error, quiet=_is_quiet(state, route))
        if not logger.isEnabledFor(level):
            return
        fields = self._fields(
            scope,
            state=state,
            asked=asked,
            status=status,
            error=error,
            duration=duration,
        )
        method = scope.get("method", "")
        logger.log(
            level,
            "%s %s %s",
            method,
            asked,
            status if status is not None else "-",
            extra=fields,
        )

    def _fields(
        self,
        scope: Scope,
        *,
        state: _State,
        asked: str,
        status: int | None,
        error: BaseException | None,
        duration: float,
    ) -> dict[str, Any]:
        """Build the record's fields, leaving out what there is none of.

        A field nothing answered is absent rather than null, the way the
        health report leaves one out, so reading a key means the request
        carried it.
        """
        fields: dict[str, Any] = {
            "http.request.method": scope.get("method", ""),
            "url.path": asked,
            "url.scheme": scope.get("scheme", "http"),
            "http.server.request.duration": round(duration, 6),
        }
        if status is not None:
            fields["http.response.status_code"] = status
        template = _route_template(scope, asked)
        if template is not None:
            fields["http.route"] = template
        client = _client_address(scope)
        if client is not None:
            fields["client.address"] = client
        version = scope.get("http_version")
        if version:
            fields["network.protocol.version"] = version
        if state.config.query:
            query = _query(scope)
            if query is not None:
                fields["url.query"] = query
        if state.config.user_agent:
            agent = _header(scope, b"user-agent")
            if agent is not None:
                fields["user_agent.original"] = agent
        if error is not None:
            fields["error.type"] = type(error).__qualname__
        return fields


def _is_quiet(state: _State, path: str) -> bool:
    """Return whether this path only speaks up when it fails."""
    return path in state.quiet_paths or (
        bool(state.quiet_patterns) and matches(path, state.quiet_patterns)
    )


class AccessLog(Reconfigurable[AccessLogConfig]):
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

    Every field is live: a mounted ConfigMap that adds a path to
    `GREL_ACCESS_LOG_EXCLUDE` stops the records for it on the next
    request, without a restart. Read more in [Live
    reconfiguration](../architecture/reconfigure.md).

    Read more in the [Access Log](../logging/access.md) docs.
    """

    kind: ClassVar[str] = "access_log"
    singleton: ClassVar[bool] = True
    asgi_observes: ClassVar[bool] = True
    singleton_reason: ClassVar[str] = (
        "One access log answers for the whole app, so two would write every "
        "request twice"
    )

    def __init__(
        self,
        *,
        include: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Paths to log. Empty (the default) means every path. A "
                "pattern ending in `*` matches as a prefix. Reads "
                "`GREL_ACCESS_LOG_INCLUDE` when unset."
            ),
        ] = None,
        exclude: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Paths to leave alone, whatever `include` says. Nothing is "
                "written for them, at any level. Reads "
                "`GREL_ACCESS_LOG_EXCLUDE` when unset."
            ),
        ] = None,
        quiet: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Paths logged at debug while they answer, and at the level "
                "their status earns when they do not. Defaults to the probe "
                "paths and `/metrics`. Pass `()` to log them like anything "
                "else."
            ),
        ] = None,
        query: Annotated[
            bool | None,
            Doc(
                "Whether the record carries `url.query`. Redacted through "
                "the same rules the rest of the library redacts a URL with, "
                "so a token in a query string never reaches the sink."
            ),
        ] = None,
        user_agent: Annotated[
            bool | None,
            Doc("Whether the record carries `user_agent.original`."),
        ] = None,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
        env_prefix: Annotated[
            str | None,
            Doc(
                "Override the derived prefix, `GREL_ACCESS_LOG_` for the "
                "default instance."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the "
                "process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> None:
        """Initialize the component with what to log and what to leave out."""
        resolved_env_prefix, kind_prefix = env_prefixes(
            "ACCESS_LOG", name, env_prefix
        )
        config = resolve_config(
            AccessLogConfig,
            explicit=None,
            kwargs={
                "include": include,
                "exclude": exclude,
                "quiet": quiet,
                "query": query,
                "user_agent": user_agent,
            },
            env_prefix=resolved_env_prefix,
            kind_env_prefix=kind_prefix,
            env_load=env_load,
        )
        self._setup(config, name=name)
        self._track_reconfigure(resolved_env_prefix)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            AccessLogConfig,
            Doc("The pre-built access log configuration."),
        ],
        *,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
    ) -> AccessLog:
        """Build the component from a configuration that is already whole.

        The one declarative door. What you pass is what runs: no
        environment variable is read, and the instance is not registered
        for live reload.
        """
        instance = cls.__new__(cls)
        instance._setup(config, name=name)  # noqa: SLF001
        return instance

    def _setup(self, config: AccessLogConfig, *, name: str) -> None:
        """Hold the configuration and the cell the middleware reads."""
        self._name = name
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._live: Live[_State] = Live(_state_of(config))
        self._silence = _Silence()
        self._wired = False

    async def _apply_reconfigure(self, new_config: AccessLogConfig) -> None:
        """Publish the snapshot the next request reads.

        One assignment, so a request either answers entirely from the
        previous configuration or entirely from this one.
        """
        self._live.state = _state_of(new_config)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with.

        Being asked is what says this app serves HTTP, which is what makes
        uvicorn's access log worth silencing. A FastStream app serves
        none, so `install` never asks, and its uvicorn access log is left
        exactly where it was.

        The middleware is handed the cell rather than the values, so a
        live reconfigure reaches it without the stack being rebuilt,
        which a framework will not do once it is serving.
        """
        self._wired = True
        return AccessLogMiddleware, {"live": self._live}

    async def __aenter__(self) -> Self:
        """Silence uvicorn's access log for as long as this one is open.

        Only once the middleware has been wired, which `micro.install(app)`
        does for a framework that serves HTTP. An app that serves none
        keeps whatever it had, because nothing here is going to write an
        access record in its place.
        """
        if self._wired:
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


def _level_of(
    status: int | None, *, error: BaseException | None, quiet: bool
) -> int:
    """Return the level a finished request is written at.

    The answer decides it: a `5xx` is the service failing, a `4xx` is the
    caller being turned away, and anything else is what the service is
    for. A quiet path that answered says nothing at info.

    A request that broke after its headers went out carries a status that
    reads fine and an exception that does not, and the exception is the
    part worth reading. A cancelled one is neither: the caller hung up, or
    the process is stopping, and that is debug on a busy port.
    """
    if isinstance(error, asyncio.CancelledError):
        return logging.DEBUG
    if error is not None:
        return logging.ERROR
    if status is None:
        return logging.DEBUG
    if status >= _SERVER_ERROR:
        return logging.ERROR
    if status >= _CLIENT_ERROR:
        return logging.WARNING
    return logging.DEBUG if quiet else logging.INFO


def _route_template(scope: Scope, asked: str) -> str | None:
    """Return the route template the request matched, when there is one.

    Read after the app has answered, because that is when the router has
    written what it matched into the scope. There is no standard key for
    it, so each framework is read the way it records it: Litestar writes
    `path_template`, and FastAPI a route carrying `path_format`. Starlette
    records neither, so a plain Starlette app leaves the field out rather
    than guessing a template from the values that filled it.

    A mount prefix goes back on, so the route reads as the path it
    grouped, which is what `url.path` on the same record carries. A proxy
    that strips its own prefix leaves `root_path` set and the path without
    it, and there the prefix stays off, for the same reason: the two
    fields describe one request and have to agree.
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
    if not root or not asked.startswith(root):
        return template
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
