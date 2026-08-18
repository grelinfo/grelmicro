"""FastAPI integration: middleware, install helper, and health router."""

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from typing_extensions import Doc

from grelmicro._app import AmbientBindingError
from grelmicro._json import json_dumps_bytes
from grelmicro.errors import OutOfContextError
from grelmicro.health._checks import HealthChecks
from grelmicro.health._models import CheckResult, HealthStatus
from grelmicro.http._problem import (
    _IN_FLIGHT_RETRY_AFTER,
    HANDLED,
    IDEMPOTENCY_IN_FLIGHT,
    IDEMPOTENCY_KEY_INVALID,
    IDEMPOTENCY_KEY_REUSED,
    PROBLEM_MEDIA_TYPE,
    REQUEST_BODY_TOO_LARGE,
    ProblemDetail,
    _Kind,
    build,
    send_problem,
)
from grelmicro.idempotency.errors import (
    IdempotencyConflictError,
    IdempotencyKeyMakerError,
    IdempotencyWaitTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Collection,
        MutableMapping,
        Sequence,
    )

    from fastapi import APIRouter, FastAPI
    from fastapi.params import Depends
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response

    from grelmicro import Grelmicro
    from grelmicro.http import ErrorResponses
    from grelmicro.idempotency import Idempotency
    from grelmicro.trace._component import Trace

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "CheckResultResponse",
    "GrelmicroMiddleware",
    "HealthzResponse",
    "IdempotencyMiddleware",
    "StoredResponse",
    "document_idempotency",
    "health_router",
    "install",
    "install_error_responses",
    "is_bound",
]

_logger = logging.getLogger(__name__)


def _instrument_app(app: "Starlette", micro: "Grelmicro") -> None:
    """Auto-instrument the FastAPI app per `Trace(instrument=...)`.

    Runs at install time, before the app serves, because the framework builds
    its middleware stack on first use and the request-span middleware must be
    in place by then. With no explicit `TracerProvider`, OTel's proxy tracer
    resolves to the provider `Trace` installs during the lifespan, so request
    spans land in grelmicro's pipeline. It is a no-op without
    `opentelemetry-instrumentation-fastapi` installed.
    """
    from grelmicro.trace._autoinstrument import (  # noqa: PLC0415
        explicit_names,
        is_selected,
    )

    component = next(
        (c for c in micro.components if getattr(c, "kind", None) == "trace"),
        None,
    )
    if component is None:
        return
    trace = cast("Trace", component)
    if not trace.active:
        # Auto-disabled Trace installs no provider, so request spans would go
        # nowhere. Skip instrumentation until an exporter endpoint is set.
        return
    directive = trace.instrument
    if not is_selected("fastapi", directive):
        return
    try:
        from fastapi import FastAPI  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return
    if not isinstance(app, FastAPI):
        return
    try:
        from opentelemetry.instrumentation.fastapi import (  # noqa: PLC0415
            FastAPIInstrumentor,
        )
    except ImportError:  # pragma: no cover
        names = explicit_names(directive)
        if names is not None and "fastapi" in names:
            _logger.warning(
                "Trace named 'fastapi' for instrumentation but "
                "opentelemetry-instrumentation-fastapi is not installed."
            )
        return
    FastAPIInstrumentor.instrument_app(app)


class GrelmicroMiddleware:
    """Bind the active `Grelmicro` app for the duration of each request.

    A request handler runs in its own task, outside the `async with micro:`
    block, so `Grelmicro.current()` and the ambient `backend=` resolution it
    powers do not see the app there. This middleware sets the active app for
    the request task, so `Lock("cart")`, `RateLimiter.sliding_window(...)`,
    and `@cached` resolve ambiently inside the handler exactly as they do in
    a task.

    ```python
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.integrations.fastapi import GrelmicroMiddleware

    micro = Grelmicro(uses=[...])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with micro:
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(GrelmicroMiddleware, micro=micro)
    ```

    Open the app in the framework lifespan so its components are registered
    before any request arrives. The middleware is pure ASGI and works with
    any ASGI framework (Starlette, Litestar, ...). It binds on `http` and
    `websocket` scopes and passes the `lifespan` scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[
            "ASGIApp",
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        micro: Annotated[
            "Grelmicro",
            Doc(
                "The `Grelmicro` app to bind for each request. Open it in "
                "the framework lifespan so its components are ready."
            ),
        ],
    ) -> None:
        """Initialize the middleware with the app to bind."""
        self.app = app
        self.micro = micro

    async def __call__(
        self, scope: "Scope", receive: "Receive", send: "Send"
    ) -> None:
        """Bind the app on request scopes, pass other scopes through."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = self.micro._bind_current()  # noqa: SLF001
        try:
            await self.app(scope, receive, send)
        finally:
            self.micro._reset_current(token)  # noqa: SLF001


def install(
    app: Annotated[
        "Starlette",
        Doc("The Starlette or FastAPI application to wire."),
    ],
    micro: Annotated[
        "Grelmicro",
        Doc(
            "The `Grelmicro` app to open in the lifespan and bind per request."
        ),
    ],
    *,
    ambient: Annotated[
        bool,
        Doc(
            "Add `GrelmicroMiddleware` so patterns resolve ambiently inside "
            "request handlers. Default `True`. Pass `False` to skip it."
        ),
    ] = True,
) -> None:
    """Wire `micro` into a Starlette or FastAPI app.

    Chains `async with micro:` around the app's existing lifespan, so any
    lifespan already passed to the framework keeps running and the components
    are open before the first request. When `ambient` is `True`, adds
    `GrelmicroMiddleware` so patterns resolve through `Grelmicro.current()`
    inside request handlers, and keeps it outside every other middleware
    however they were added, so one that resolves a backend ambiently, such
    as `IdempotencyMiddleware`, always runs inside the request scope. The
    placement is read back on startup and raises `AmbientBindingError` if it
    did not hold.

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: "Starlette") -> "AsyncIterator[Any]":
        _check_binding_outermost(app)
        async with previous(app) as state, micro:
            yield state

    app.router.lifespan_context = lifespan
    if ambient:
        app.add_middleware(GrelmicroMiddleware, micro=micro)
        _keep_binding_outermost(app)
    else:
        micro._on_ambient_disabled()  # noqa: SLF001
    _instrument_app(app, micro)


def install_error_responses(
    app: Annotated[
        "Starlette",
        Doc("The Starlette or FastAPI application to wire."),
    ],
    errors: Annotated[
        "ErrorResponses",
        Doc("The registered component that renders each rejection."),
    ],
) -> None:
    """Render grelmicro rejections in a standard format on a Starlette app.

    Registers one exception handler per rejection grelmicro raises to turn a
    caller away, so a rate limiter, a bulkhead, an open circuit breaker, an
    elapsed deadline, or an idempotency conflict answers the client with an
    `application/problem+json` body instead of a `500`.

    Starlette looks a handler up through the raised exception's class
    hierarchy, so registering `AdmissionError` covers every rejection under
    it, including one a later release adds.

    `micro.install(app)` calls this when `ErrorResponses()` is registered.
    Call it directly only on an app that never goes through `install`.

    ```python
    from grelmicro.http import ErrorResponses
    from grelmicro.integrations.fastapi import install_error_responses

    install_error_responses(app, ErrorResponses())
    ```

    Read more in the [Problem Details](../http/problems.md) docs.
    """

    async def handler(request: "Request", exc: Exception) -> "Response":
        """Render one rejection, taking the occurrence from the request path."""
        from starlette.responses import Response  # noqa: PLC0415

        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    for klass in HANDLED:
        # A handler the app registered first wins. Registering before
        # `install` is the natural order (build the app, register handlers,
        # wire grelmicro), and overwriting there would take a handler away
        # from an app that upgrades, without saying so.
        if klass not in app.exception_handlers:
            app.add_exception_handler(klass, handler)


def _is_binding(middleware: object) -> bool:
    """Return whether a `user_middleware` entry is `GrelmicroMiddleware`.

    Matches the class itself and not a subclass, so only a middleware whose
    whole body is the binding is moved.
    """
    return getattr(middleware, "cls", None) is GrelmicroMiddleware


def _check_binding_outermost(app: "Starlette") -> None:
    """Raise when a registered `GrelmicroMiddleware` is not the outermost one.

    Runs once the stack is built. Reading the order back turns a placement
    that did not hold into a failure at startup, rather than an
    `OutOfContextError` on the first request that resolves a backend.
    """
    binding = [
        index
        for index, middleware in enumerate(app.user_middleware)
        if _is_binding(middleware)
    ]
    if binding and binding[0] != 0:
        outer = getattr(app.user_middleware[0].cls, "__name__", "A middleware")
        msg = (
            f"{outer} wraps GrelmicroMiddleware, so a middleware that "
            f"resolves a backend ambiently, such as IdempotencyMiddleware, "
            f"raises OutOfContextError on every request that reaches it. "
            f"Add GrelmicroMiddleware last, or let micro.install(app) "
            f"place it."
        )
        raise AmbientBindingError(msg)


def _keep_binding_outermost(app: "Starlette") -> None:
    """Move `GrelmicroMiddleware` to the front of the stack as it is built.

    The framework builds its middleware stack once, on the first request or
    lifespan event, from `app.user_middleware`. Reordering that list at build
    time puts the binding middleware outside every other one, so a middleware
    added after `install` still runs inside the grelmicro request scope.
    """
    build = app.build_middleware_stack

    def build_middleware_stack() -> "ASGIApp":
        current = app.user_middleware
        app.user_middleware = [
            *(m for m in current if _is_binding(m)),
            *(m for m in current if not _is_binding(m)),
        ]
        return build()

    app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]


_MAX_KEY_LENGTH = 255
"""Longest accepted idempotency key, in characters.

A longer key is answered with `400`.
"""

_REPLAY_HEADER = b"idempotent-replayed"
"""Response header marking a replayed response."""

_MIN_CONTENT_STATUS = 200
"""Lowest status that may carry content."""

_BODYLESS_STATUSES = frozenset({204, 304})
"""Statuses that carry no content, so a replay sends no Content-Length."""

_KEY_SEPARATOR = "\x1f"
"""Separator joining the parts of a stored key."""


class StoredResponse(TypedDict):
    """The response `IdempotencyMiddleware` is about to store.

    Handed to `skip` so a handler's own rule decides whether a response
    replays. `headers` maps lowercased names to their value, keeping the
    last of a repeated name.
    """

    status: int
    headers: dict[str, str]
    body: bytes


class _Entry(TypedDict):
    """A stored response as it rides the cache.

    Header values and the body are `latin-1` strings, which round-trip
    any byte sequence through the cache serializers without loss.
    """

    status: int
    headers: "Sequence[Sequence[str]]"
    body: str


class IdempotencyMiddleware:
    """Replay a stored HTTP response when a request repeats its idempotency key.

    A request whose method is listed in `methods` and which carries the
    `header` runs once. A retry with the same key replays the stored
    status, headers, and body without reaching the handler, and carries
    `idempotent-replayed: true`. A request without the header passes
    straight through, so adding the middleware changes nothing until a
    client opts in.

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.idempotency import Idempotency
    from grelmicro.integrations.fastapi import IdempotencyMiddleware

    micro = Grelmicro(uses=[...])
    app = FastAPI()
    micro.install(app)

    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=3600)
    )
    ```

    Add it before or after `micro.install(app)`. It resolves its `Cache`
    through the grelmicro request scope, which `install` keeps outside
    every other middleware.

    A duplicate that arrives while the first execution is in flight waits
    for it and replays its response. The wait folds across replicas when
    a `Coordination` lock backend is configured, and in-process
    otherwise. It is bounded by `wait_timeout`.

    Every response the app returns is stored, errors included. A handler
    that raises an unhandled exception stores nothing, so the framework's
    `500` never replays.

    Four kinds of response are not stored, and each one lets a retry
    re-run the handler: one carrying `Set-Cookie`, one carrying
    `Content-Encoding`, one declaring trailers, and one whose body is
    over `max_body_size`. All four are logged. Pass `skip` to add a rule
    of your own.

    Background tasks run after the response is sent, so a replay can be
    served while the original request's background work is still in
    flight.

    The middleware is pure ASGI and works with any ASGI framework
    (Starlette, Litestar, ...). It acts on `http` scopes and passes every
    other scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[
            "ASGIApp",
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        idempotency: Annotated[
            "Idempotency[Any]",
            Doc(
                "The `Idempotency` that stores responses. Its `ttl` sets "
                "how long a key replays."
            ),
        ],
        header: Annotated[
            str,
            Doc("Request header carrying the idempotency key."),
        ] = "Idempotency-Key",
        methods: Annotated[
            "Collection[str]",
            Doc(
                "Methods that take an idempotency key. Every other method "
                "passes through."
            ),
        ] = ("POST",),
        key_maker: Annotated[
            "Callable[[Scope, str], str] | None",
            Doc(
                """
                Build the stored key from the ASGI scope and the client key.

                Defaults to the method, the path, the query string, and
                the client key, so two routes never replay each other.
                **Set this in any multi-tenant app**, folding in the
                caller identity. Without it a client that learns another
                client's key replays their response.
                """
            ),
        ] = None,
        skip: Annotated[
            "Callable[[StoredResponse], bool] | None",
            Doc(
                """
                Predicate receiving the response. Return `True` to not store it.

                Mirrors `skip` on `@cached`. Use it for a response that
                is technically replayable but should not be, such as one
                whose body embeds a timestamp the caller must not see
                twice. Responses that are never safe to replay are
                dropped before this runs.
                """
            ),
        ] = None,
        require_key: Annotated[
            bool,
            Doc(
                "Answer `400` when a method in `methods` arrives without "
                "the header, instead of passing it through."
            ),
        ] = False,
        fingerprint_body: Annotated[
            bool,
            Doc(
                """
                Hash the request body and store the hash with the response.

                A key reused with a different body then gets `422` instead
                of a wrong replay. Buffers the request body before the
                handler runs, and answers `413` when it is over
                `max_body_size`.
                """
            ),
        ] = False,
        max_body_size: Annotated[
            int,
            Doc(
                "Largest body held in memory, in bytes. A larger response "
                "is sent to the client and not stored. With "
                "`fingerprint_body`, a larger request body is answered "
                "with `413`."
            ),
        ] = 1024 * 1024,
        wait_timeout: Annotated[
            float,
            Doc(
                """
                Seconds a duplicate waits for an execution already in flight.

                Past it the duplicate is answered with `409` and a
                `Retry-After` header.
                """
            ),
        ] = 10.0,
    ) -> None:
        """Initialize the middleware with the idempotency store and policy."""
        self.app = app
        self._idempotency = idempotency
        self._header = header.lower().encode("latin-1")
        self._header_name = header
        self._methods = frozenset(method.upper() for method in methods)
        self._key_maker = key_maker
        self._skip = skip
        self._require_key = require_key
        self._fingerprint_body = fingerprint_body
        self._max_body_size = max_body_size
        self._wait_timeout = wait_timeout

    async def __call__(
        self, scope: "Scope", receive: "Receive", send: "Send"
    ) -> None:
        """Replay, execute, or pass the request through."""
        if scope["type"] != "http" or scope["method"] not in self._methods:
            await self.app(scope, receive, send)
            return

        key = _header_value(scope["headers"], self._header)
        if not key:
            if self._require_key:
                await _send_problem(
                    send,
                    scope,
                    IDEMPOTENCY_KEY_INVALID,
                    f"The {self._header_name} header is required on this "
                    f"request and was not sent.",
                )
                return
            await self.app(scope, receive, send)
            return

        if len(key) > _MAX_KEY_LENGTH:
            await _send_problem(
                send,
                scope,
                IDEMPOTENCY_KEY_INVALID,
                f"The {self._header_name} header is longer than "
                f"{_MAX_KEY_LENGTH} characters.",
            )
            return

        fingerprint = None
        if self._fingerprint_body:
            body, too_large, receive = await _buffer_request(
                receive, self._max_body_size
            )
            if too_large:
                await _send_problem(send, scope, REQUEST_BODY_TOO_LARGE)
                return
            if body is not None:
                fingerprint = hashlib.sha256(body).hexdigest()

        await self._execute(
            scope, receive, send, self._storage_key(scope, key), fingerprint
        )

    async def _execute(
        self,
        scope: "Scope",
        receive: "Receive",
        send: "Send",
        storage_key: str,
        fingerprint: str | None,
    ) -> None:
        """Run the request under the idempotency block, or replay it."""
        block = self._idempotency(
            storage_key,
            fingerprint=fingerprint,
            wait_timeout=self._wait_timeout,
        )
        try:
            operation = await block.__aenter__()
        except IdempotencyConflictError:
            await _send_problem(
                send,
                scope,
                IDEMPOTENCY_KEY_REUSED,
                f"The {self._header_name} header was already used with a "
                f"different request payload. Use a fresh key, or resend the "
                f"original payload.",
            )
            return
        except IdempotencyWaitTimeoutError:
            await _send_problem(
                send,
                scope,
                IDEMPOTENCY_IN_FLIGHT,
                f"A request with this {self._header_name} is still running. "
                f"Retry after the delay in the Retry-After header to read "
                f"its response.",
                retry_after=_IN_FLIGHT_RETRY_AFTER,
            )
            return
        except OutOfContextError as exc:
            raise OutOfContextError(_OUT_OF_CONTEXT_HINT) from exc

        try:
            if operation.replayed:
                await _send_stored(
                    send, operation.result(), head=scope["method"] == "HEAD"
                )
            else:
                capture = _ResponseCapture(
                    send, self._max_body_size, self._skip
                )
                await self.app(scope, receive, capture)
                if capture.stored is not None:
                    operation.store(capture.stored)
        except BaseException as exc:
            await block.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await block.__aexit__(None, None, None)

    def _storage_key(self, scope: "Scope", key: str) -> str:
        """Build the stored key, scoped by route unless `key_maker` says otherwise."""
        if self._key_maker is not None:
            return _checked_key(self._key_maker(scope, key), key)
        parts = [scope["method"], scope["path"]]
        query = scope.get("query_string", b"")
        if query:
            parts.append(query.decode("latin-1"))
        parts.append(key)
        return _KEY_SEPARATOR.join(parts)


def document_idempotency(
    app: Annotated[
        "FastAPI",
        Doc("The app carrying an `IdempotencyMiddleware` to document."),
    ],
) -> None:
    """Describe the installed `IdempotencyMiddleware` in the OpenAPI schema.

    A middleware runs outside the routing layer, so nothing it does reaches
    the generated schema and a client built from that schema never learns
    the header exists. This reads the installed middleware and annotates
    every operation it covers with the header parameter and the responses
    the middleware itself can return.

    ```python
    from grelmicro.integrations.fastapi import (
        IdempotencyMiddleware,
        document_idempotency,
    )

    app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
    micro.install(app)
    document_idempotency(app)
    ```

    Call it any time after `add_middleware`. The schema is annotated the
    next time it is built, so routes added afterwards are covered too.

    An operation that already declares the header keeps its own
    declaration. A `422` that FastAPI generated for request validation
    keeps its schema, and the idempotency case is added to its
    description.

    A mounted sub-application builds its own schema, which this does not
    reach. Call it on the sub-application as well.

    Raises:
        DependencyNotFoundError: If `fastapi` is not installed.
        TypeError: If `app` is not a `FastAPI` app, or carries no
            `IdempotencyMiddleware`.
    """
    try:
        from fastapi import FastAPI as _FastAPI  # noqa: PLC0415
    except ImportError:
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi") from None

    if not isinstance(app, _FastAPI):
        msg = (
            f"document_idempotency() needs a FastAPI app, got "
            f"{type(app).__name__}. Only FastAPI builds an OpenAPI schema."
        )
        raise TypeError(msg)

    options = _idempotency_options(app)
    original = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original()
        _annotate_schema(schema, options)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    # Drop a schema built before this call, which would otherwise be
    # served from the cache without the annotations.
    app.openapi_schema = None


def _idempotency_options(app: "FastAPI") -> dict[str, Any]:
    """Return the installed middleware's arguments, defaults filled in."""
    import inspect  # noqa: PLC0415

    for middleware in app.user_middleware:
        # `add_middleware` prepends, so the first match is the last added,
        # which is the outermost at runtime and answers first on the wire.
        cls = middleware.cls
        if isinstance(cls, type) and issubclass(cls, IdempotencyMiddleware):
            # Every parameter after `app` is keyword-only, so `add_middleware`
            # can only have passed them by keyword.
            bound = inspect.signature(IdempotencyMiddleware).bind_partial(
                **middleware.kwargs
            )
            bound.apply_defaults()
            return dict(bound.arguments)
    msg = (
        "document_idempotency() found no IdempotencyMiddleware on the app. "
        "Add it with app.add_middleware(IdempotencyMiddleware, ...) first."
    )
    raise TypeError(msg)


def _annotate_schema(schema: dict[str, Any], options: dict[str, Any]) -> None:
    """Add the header and the middleware's responses to covered operations."""
    methods = {method.lower() for method in options["methods"]}
    header = options["header"]
    fingerprint_body = options["fingerprint_body"]
    parameter = {
        "name": header,
        "in": "header",
        "required": options["require_key"],
        "schema": {"type": "string", "maxLength": _MAX_KEY_LENGTH},
        "description": (
            "Key that makes this request safe to retry. A repeat within the "
            "replay window returns the first response instead of running the "
            f"operation again. Up to {_MAX_KEY_LENGTH} ASCII characters."
        ),
    }
    responses = {
        "400": f"`{header}` is missing or longer than {_MAX_KEY_LENGTH} characters."
        if options["require_key"]
        else f"`{header}` is longer than {_MAX_KEY_LENGTH} characters.",
        "409": (
            f"A request with this `{header}` is still in flight. Retry after "
            f"the delay in `Retry-After`."
        ),
    }
    if fingerprint_body:
        responses["413"] = "Request body too large to fingerprint."
        responses["422"] = (
            f"This `{header}` was already used with a different request "
            f"payload."
        )

    covered = [
        (path_item, operation)
        for path_item in schema.get("paths", {}).values()
        for method, operation in path_item.items()
        # Every non-operation key of a path item (`parameters`, `servers`,
        # `summary`, `$ref`) holds a list or a string, so a mapping under a
        # covered method is an operation.
        if method.lower() in methods and isinstance(operation, dict)
    ]
    if not covered:
        return
    ref = _add_problem_schema(schema)
    for path_item, operation in covered:
        _add_parameter(operation, path_item, parameter)
        for status, description in responses.items():
            _merge_response(operation, status, description, ref)


def _add_problem_schema(schema: dict[str, Any]) -> str:
    """Publish the problem body component and return the ref that points at it.

    An app may already publish a model of its own under the same name, in
    which case pointing the middleware's responses at it would hand a
    generated client the wrong shape to decode. The component is compared
    before it is reused, and a different one is published beside it under a
    qualified name rather than replacing what the app declared.
    """
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    ours = ProblemDetail.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    for name in (_PROBLEM_SCHEMA_NAME, _QUALIFIED_PROBLEM_SCHEMA_NAME):
        existing = schemas.get(name)
        if existing is None:
            schemas[name] = ours
            return f"#/components/schemas/{name}"
        if existing == ours:
            return f"#/components/schemas/{name}"
    # Both names are taken by something else, which takes a deliberate act.
    # Say nothing about the body rather than name the wrong shape.
    return ""


def _add_parameter(
    operation: dict[str, Any],
    path_item: dict[str, Any],
    parameter: dict[str, Any],
) -> None:
    """Add the header parameter unless the operation already declares it.

    OpenAPI keys a parameter by name and location, and forbids the same
    pair twice, so a declaration already present at either level wins.
    """
    name = parameter["name"].lower()
    declared = [
        *operation.get("parameters", ()),
        *path_item.get("parameters", ()),
    ]
    if any(
        existing.get("in") == "header"
        and str(existing.get("name", "")).lower() == name
        for existing in declared
    ):
        return
    # A copy per operation, so post-processing one never edits the rest.
    operation.setdefault("parameters", []).append(dict(parameter))


def _merge_response(
    operation: dict[str, Any], status: str, description: str, ref: str
) -> None:
    """Describe a status the middleware returns, keeping what is there.

    FastAPI generates a `422` carrying the validation error schema. That
    entry keeps its schema and gains this description and the problem
    media type alongside it, so neither case is lost and a second call
    adds nothing.
    """
    responses = operation.setdefault("responses", {})
    existing = responses.get(status)
    if existing is None:
        responses[status] = {"description": description}
        existing = responses[status]
    else:
        current = existing.get("description", "")
        if description not in current:
            existing["description"] = f"{current}\n\n{description}".strip()
    if ref:
        existing.setdefault("content", {}).setdefault(
            PROBLEM_MEDIA_TYPE, {"schema": {"$ref": ref}}
        )


_PROBLEM_SCHEMA_NAME = "ProblemDetail"
"""Name the problem body is published under in the OpenAPI components."""

_QUALIFIED_PROBLEM_SCHEMA_NAME = "GrelmicroProblemDetail"
"""Fallback name, for an app that publishes a `ProblemDetail` of its own."""


_UNRESOLVED_TOKEN = re.compile(r"(?:^|[^0-9A-Za-z_])None(?:$|[^0-9A-Za-z_])")
"""A formatted `None` sitting on its own between separators in a key."""


def _checked_key(built: object, client_key: str) -> str:
    """Return `built`, or raise when it cannot separate one caller from another.

    A key that is partly missing does not fail, it merges. Every caller whose
    key lost the same component lands in one entry, and the request still
    answers normally, so the widening is invisible. That is a confidentiality
    boundary quietly removed, which is worth refusing over.

    Raises:
        IdempotencyKeyMakerError: If the key is not a non-empty string, drops
            the client's key, or carries an unresolved `None`.
    """
    if not isinstance(built, str) or not built:
        msg = f"key_maker returned {built!r}, expected a non-empty string."
        raise IdempotencyKeyMakerError(msg)
    if client_key not in built:
        msg = (
            f"key_maker returned {built!r}, which drops the client's "
            f"idempotency key. Every request to this route would then share "
            f"one entry. Include the key it was given."
        )
        raise IdempotencyKeyMakerError(msg)
    if _UNRESOLVED_TOKEN.search(built):
        msg = (
            f"key_maker returned {built!r}, which carries an unresolved None. "
            f"Something the key reads was not set yet, so that component is "
            f"the same for every caller and they share one entry. A middleware "
            f"the key depends on must run outside IdempotencyMiddleware, which "
            f"means adding it after."
        )
        raise IdempotencyKeyMakerError(msg)
    return built


_OUT_OF_CONTEXT_HINT = (
    "IdempotencyMiddleware resolved no cache backend. Call micro.install(app) "
    "so the grelmicro request scope wraps it, register a Cache component, or "
    "pass an explicit cache= to Idempotency."
)


class _ResponseCapture:
    """Forward an ASGI response downstream while copying it for storage.

    Each chunk reaches the client as the handler produces it, so storing
    a response adds no latency. `stored` stays None until the final body
    message arrives, so a response torn off midway is never replayed.
    """

    def __init__(
        self,
        send: "Send",
        max_body_size: int,
        skip: "Callable[[StoredResponse], bool] | None" = None,
    ) -> None:
        """Initialize the capture around the downstream `send`."""
        self._send = send
        self._max_body_size = max_body_size
        self._skip = skip
        self._status = 0
        self._headers: list[tuple[str, str]] = []
        self._chunks: list[bytes] = []
        self._size = 0
        self._storable = False
        self.stored: _Entry | None = None

    async def __call__(self, message: "Message") -> None:
        """Capture the message, then forward it downstream."""
        if message["type"] == "http.response.start":
            self._start(message)
        elif message["type"] == "http.response.body":
            self._body(message)
        await self._send(message)

    def _start(self, message: "Message") -> None:
        """Record the status and headers, and decide whether to store."""
        blockers: list[str] = []
        for name, _value in message["headers"]:
            lowered = name.lower()
            if lowered == b"set-cookie":
                blockers.append("Set-Cookie")
            elif lowered == b"content-encoding":
                blockers.append("Content-Encoding")
        if message.get("trailers"):
            blockers.append("trailers")
        self._status = message["status"]
        # Content-Length is recomputed on replay, so a stored value that
        # drifts from the stored body can never reach a client.
        self._headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in message["headers"]
            if name.lower() != b"content-length"
        ]
        self._storable = not blockers
        if blockers:
            _logger.warning(
                "Idempotent response not stored: it carries %s. A retry with "
                "the same key will run the handler again.",
                " and ".join(blockers),
            )

    def _body(self, message: "Message") -> None:
        """Accumulate the body, or give up once it outgrows the limit."""
        if not self._storable:
            return
        chunk = message.get("body", b"")
        self._size += len(chunk)
        if self._size > self._max_body_size:
            self._storable = False
            self._chunks.clear()
            _logger.warning(
                "Idempotent response not stored: body exceeds max_body_size "
                "(%d bytes). A retry with the same key will run the handler "
                "again.",
                self._max_body_size,
            )
            return
        self._chunks.append(chunk)
        if message.get("more_body", False):
            return
        body = b"".join(self._chunks)
        if self._skip is not None and self._skip(
            StoredResponse(
                status=self._status,
                headers=dict(self._headers),
                body=body,
            )
        ):
            return
        self.stored = _Entry(
            status=self._status,
            headers=self._headers,
            body=body.decode("latin-1"),
        )


def _header_value(
    headers: "Sequence[tuple[bytes, bytes]]", name: bytes
) -> str | None:
    """Return the first value of `name`, or None when it is absent."""
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1").strip()
    return None


async def _buffer_request(
    receive: "Receive", max_body_size: int
) -> "tuple[bytes | None, bool, Receive]":
    """Read the request body, and return it with a receive that replays it.

    Returns `(body, too_large, receive)`. `body` is None when the client
    disconnected before the last chunk, so the caller fingerprints
    nothing rather than hashing a truncated payload as if it were whole.
    `too_large` reports a body over `max_body_size`, which the caller
    answers with `413` instead of buffering without bound.

    The returned receive replays the consumed messages one for one,
    trailing disconnect included, so the app downstream reads exactly
    what the client sent.
    """
    consumed: list[Message] = []
    chunks: list[bytes] = []
    size = 0
    complete = False
    while True:
        message = await receive()
        consumed.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > max_body_size:
            return None, True, receive
        chunks.append(chunk)
        if not message.get("more_body", False):
            complete = True
            break
    pending = iter(consumed)

    async def replay_receive() -> "Message":
        message = next(pending, None)
        if message is None:
            return await receive()
        return message

    return (b"".join(chunks) if complete else None), False, replay_receive


async def _send_stored(send: "Send", stored: _Entry, *, head: bool) -> None:
    """Send a stored response, marked as a replay.

    Content-Length is recomputed from the stored body, and left off the
    statuses that carry no content, so a replay stays a valid response.
    """
    body = stored["body"].encode("latin-1")
    status = stored["status"]
    headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in stored["headers"]
    ]
    if status >= _MIN_CONTENT_STATUS and status not in _BODYLESS_STATUSES:
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
    headers.append((_REPLAY_HEADER, b"true"))
    await send(
        {
            "type": "http.response.start",
            "status": stored["status"],
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": b"" if head else body})


async def _send_problem(
    send: "Send",
    scope: "Scope",
    kind: _Kind,
    detail: str | None = None,
    *,
    retry_after: float | None = None,
) -> None:
    """Answer a request the middleware refuses itself, before the app runs.

    A middleware sits outside the routing layer, so no exception handler
    sees what it decides. It writes the same problem detail a raised
    rejection would produce, in the same shape and media type.
    """
    extensions: dict[str, Any] | None = (
        None if retry_after is None else {"retry_after": retry_after}
    )
    await send_problem(
        send,
        build(
            kind,
            detail=detail,
            instance=scope["path"],
            extensions=extensions,
        ),
    )


_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _always_true() -> bool:
    return True


def _always_false() -> bool:
    return False


def _omitted_when_absent(schema: dict[str, Any]) -> None:
    """Drop the `null` default from a field the response omits when unset."""
    schema.pop("default", None)


class CheckResultResponse(BaseModel):
    """Health status of a single check.

    `error` is present only on a failing check, and `details` only when the
    check returned some and the router is showing them. Both are absent
    otherwise rather than sent as `null`, so the schema types them as a plain
    string and object.
    """

    status: HealthStatus
    critical: bool = True
    error: Annotated[
        str | SkipJsonSchema[None],
        Field(default=None, json_schema_extra=_omitted_when_absent),
    ]
    details: Annotated[
        dict[str, Any] | SkipJsonSchema[None],
        Field(default=None, json_schema_extra=_omitted_when_absent),
    ]


class HealthzResponse(BaseModel):
    """Aggregate health report."""

    status: HealthStatus
    checks: dict[str, CheckResultResponse]


def health_router(
    component: Annotated[
        HealthChecks | None,
        Doc(
            "Health checks component whose checks the router runs. When "
            "omitted, the router resolves the default instance from the "
            "active `Grelmicro` app (``Grelmicro(uses=[HealthChecks(...)])``)."
        ),
    ] = None,
    *,
    prefix: Annotated[
        str,
        Doc("URL prefix for health endpoints (e.g. '/api/v1')."),
    ] = "",
    show_details: Annotated[
        "bool | Depends",
        Doc(
            "Whether ``/healthz`` includes each check's verbose "
            "``details`` field (versions, hostnames, pool stats, ...):\n\n"
            "- ``False`` (default): details are stripped. Safe for "
            "public endpoints.\n"
            "- ``True``: details are always included. Use only if "
            "``/healthz`` is private.\n"
            "- ``Depends(fn)`` where ``fn`` returns ``bool``: wires "
            "``fn`` into FastAPI's DI graph, so ``Depends`` chains, "
            "``yield`` cleanup, ``Security``, ``Request`` injection, "
            "and async all work naturally. Return ``True`` to show "
            "details, ``False`` to strip them. Raising "
            "``HTTPException`` blocks the endpoint, so return "
            "``False`` instead when you want a soft strip."
        ),
    ] = False,
    healthz_dependencies: Annotated[
        "list[Depends] | None",
        Doc(
            "FastAPI dependencies applied to ``/healthz``. A failing "
            "dependency blocks the entire endpoint (``401``/``403``). "
            "Use to hide ``/healthz`` from the public while leaving "
            "``/livez`` and ``/readyz`` open to orchestrators and "
            "load balancers. Independent of ``show_details``."
        ),
    ] = None,
) -> "APIRouter":
    """Create a FastAPI router with health check endpoints.

    Provides three endpoints:

    - ``GET/HEAD {prefix}/livez``: Liveness probe. Never runs
      checkers. Always returns ``200`` with an empty body.
    - ``GET/HEAD {prefix}/readyz``: Readiness probe. Runs critical
      checkers only. Returns ``200`` or ``503`` with an empty body.
    - ``GET/HEAD {prefix}/healthz``: Aggregate JSON report.

    All responses set ``Cache-Control: no-store``.

    Raises:
        DependencyNotFoundError: If ``fastapi`` is not installed.
        TypeError: If ``show_details`` is neither a bool nor a
            ``Depends(...)`` value.
    """
    try:
        from fastapi import APIRouter as _APIRouter  # noqa: PLC0415
        from fastapi import Depends, Query  # noqa: PLC0415
        from fastapi.responses import Response  # noqa: PLC0415
        from starlette.status import (  # noqa: PLC0415
            HTTP_200_OK,
            HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ImportError:
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")  # noqa: B904

    from grelmicro._app import Grelmicro  # noqa: PLC0415

    def _resolve_component() -> "HealthChecks":
        return component or Grelmicro.current().get("health", "default")

    show_details_dep = _resolve_show_details_dep(show_details)

    router = _APIRouter(prefix=prefix, tags=["health"])
    healthz_deps = list(healthz_dependencies or ())

    @router.get("/livez", status_code=HTTP_200_OK)
    @router.head("/livez", include_in_schema=False)
    async def livez() -> Response:
        """Liveness probe. Always returns ``200`` with an empty body."""
        return Response(status_code=HTTP_200_OK, headers=_NO_STORE_HEADERS)

    @router.get(
        "/readyz",
        status_code=HTTP_200_OK,
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "description": (
                    "At least one critical component is unhealthy."
                ),
            },
        },
    )
    @router.head("/readyz", include_in_schema=False)
    async def readyz(
        exclude: Annotated[
            str | None,
            Query(
                description="Comma-separated list of checker names to skip.",
            ),
        ] = None,
    ) -> Response:
        """Readiness probe. Runs critical checkers only."""
        report = await _resolve_component().run(
            critical_only=True,
            exclude=_parse_exclude(exclude),
        )
        status_code = (
            HTTP_200_OK
            if report["status"] == HealthStatus.OK
            else HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(status_code=status_code, headers=_NO_STORE_HEADERS)

    @router.get(
        "/healthz",
        response_model=HealthzResponse,
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "model": HealthzResponse,
                "description": "At least one critical component is unhealthy.",
            },
        },
        dependencies=healthz_deps,
    )
    @router.head("/healthz", include_in_schema=False, dependencies=healthz_deps)
    async def healthz(
        include_details: Annotated[bool, Depends(show_details_dep)],
        exclude: Annotated[
            str | None,
            Query(
                description="Comma-separated list of checker names to skip.",
            ),
        ] = None,
    ) -> Response:
        """Aggregate JSON report of all checker results."""
        report = await _resolve_component().run(
            critical_only=False,
            exclude=_parse_exclude(exclude),
        )
        body: Any = {
            "status": report["status"],
            "checks": {
                name: _check_body(result, details=include_details)
                for name, result in report["checks"].items()
            },
        }
        status_code = (
            HTTP_200_OK
            if report["status"] == HealthStatus.OK
            else HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(
            content=json_dumps_bytes(body),
            status_code=status_code,
            media_type="application/json",
            headers=_NO_STORE_HEADERS,
        )

    return router


def _check_body(result: CheckResult, *, details: bool) -> dict[str, Any]:
    """Build one `/healthz` check entry, leaving out what carries no value.

    A passing check reports `status` and `critical` alone. `error` is added
    only when the check failed, and `details` only when the check returned
    some and the router is showing them.
    """
    body: dict[str, Any] = {
        "status": result["status"],
        "critical": result["critical"],
    }
    error = result["error"]
    if error is not None:
        body["error"] = error
    if details:
        check_details = result["details"]
        if check_details is not None:
            body["details"] = check_details
    return body


def _resolve_show_details_dep(show_details: Any) -> "Callable[..., Any]":  # noqa: ANN401
    """Return the FastAPI dependency callable for ``show_details``.

    Booleans collapse to shared constant-returning helpers (identity
    stable across router builds, so FastAPI's DI can reuse them).
    ``Depends(fn)`` yields the underlying ``fn`` so FastAPI wires it
    through its DI graph on the route.
    """
    from fastapi.params import Depends as _DependsParam  # noqa: PLC0415

    if show_details is True:
        return _always_true
    if show_details is False:
        return _always_false
    if isinstance(show_details, _DependsParam):
        if show_details.dependency is None:
            msg = "show_details=Depends(None) is not allowed"
            raise TypeError(msg)
        return show_details.dependency
    msg = (
        "show_details must be bool or Depends(fn) where fn returns "
        f"bool, got {type(show_details).__name__}"
    )
    raise TypeError(msg)


def _parse_exclude(raw: str | None) -> frozenset[str]:
    """Split a comma-separated exclude list into a frozenset of names.

    ``frozenset`` so the component's ``run(exclude=...)`` can adopt it
    without copying (CPython short-circuits ``frozenset(frozenset)``
    to the same object).
    """
    if not raw:
        return frozenset()
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def is_bound(
    app: Annotated[
        "Starlette",
        Doc("The Starlette or FastAPI application to inspect."),
    ],
) -> bool:
    """Return whether `install` added the per-request binding middleware.

    Called by `Grelmicro.check_ambient_binding` and `Grelmicro.describe` to
    catch an app that never had `micro.install(app)` called on it, including
    a mounted sub-application, which otherwise resolves against the host's
    components with nothing reporting it.
    """
    return any(
        getattr(middleware, "cls", None) is GrelmicroMiddleware
        for middleware in getattr(app, "user_middleware", ())
    )
