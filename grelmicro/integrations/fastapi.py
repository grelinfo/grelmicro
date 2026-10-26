"""FastAPI integration: the Starlette wiring plus what Starlette lacks.

The lifespan, the binding, and the error responses are pure ASGI and live in
`grelmicro.integrations.starlette`. This module adds what Starlette lacks,
the OpenAPI schema and the health and metrics routers.
"""

import weakref
from collections.abc import Callable, Collection, Iterator, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final

from grelmicro.integrations._floor import (
    FASTAPI_FLOOR,
    STARLETTE_FLOOR,
    require,
)

try:
    # The module has to import without FastAPI. The versions are checked
    # before anything else is read from FastAPI.
    import fastapi
    import starlette
except ImportError:  # pragma: no cover - the reimport test walks this
    pass
else:
    require(fastapi, FASTAPI_FLOOR, extra="fastapi")
    require(starlette, STARLETTE_FLOOR, extra="fastapi")

try:
    # Its annotations and dependencies are read at runtime, so FastAPI is
    # imported here.
    from fastapi import APIRouter, FastAPI
    from fastapi import Depends as _Depends
    from fastapi.params import Depends
    from opentelemetry import trace as _otel_trace
    from starlette.middleware import Middleware
    from starlette.types import ASGIApp, Receive, Scope, Send

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - the reimport test walks this
    HAS_FASTAPI = False

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from typing_extensions import Doc

from grelmicro import (
    ComponentNotRegisteredError,
    Grelmicro,
    NoActiveAppError,
)
from grelmicro._endpoints import NO_STORE_HEADERS
from grelmicro.health._checks import HealthChecks
from grelmicro.health._endpoints import (
    JSON_MEDIA_TYPE,
    parse_exclude,
    report_body,
    status_code,
)
from grelmicro.health._models import HealthStatus
from grelmicro.health._served import (
    HealthEndpoint,
    health_endpoints_in,
    mark_health_endpoint,
)
from grelmicro.http import Gate, RouteDeclaration
from grelmicro.integrations import _fastapi_internals as _fastapi
from grelmicro.integrations._fastapi_internals import telemetry_of
from grelmicro.integrations.starlette import (
    HTTP_422_UNPROCESSABLE_CONTENT,
    _keep_watching_outside,
)
from grelmicro.integrations.starlette import (
    _front_middleware as _starlette_front_middleware,
)
from grelmicro.integrations.starlette import _wire as _wire_starlette
from grelmicro.integrations.starlette import (
    _wire_route_reading as _wire_route_reading_starlette,
)
from grelmicro.integrations.starlette import (
    install_error_responses as _install_error_responses_starlette,
)
from grelmicro.integrations.starlette import (
    install_middleware as _install_middleware_starlette,
)

if TYPE_CHECKING:
    # Each `X as X` import re-exports a name served with a warning until 0.45.0.
    from grelmicro._caller import is_authenticated as is_authenticated
    from grelmicro.http import ErrorResponses as ErrorResponses
    from grelmicro.http import (
        PreconditionRequiredError as PreconditionRequiredError,
    )
    from grelmicro.http import check_freshness as check_freshness
    from grelmicro.http._authentication import (
        declare_anonymous as declare_anonymous,
    )
    from grelmicro.http._authentication import (
        describe_app_schema as describe_app_schema,
    )
    from grelmicro.http._conditional import (
        declare_precondition_required as declare_precondition_required,
    )
    from grelmicro.http._openapi import add_error_schema as add_error_schema
    from grelmicro.http._openapi import describing as describing
    from grelmicro.http._openapi import referenced as referenced
    from grelmicro.http._ratelimit import bucket_of as bucket_of
    from grelmicro.http._ratelimit import (
        check_route_limiters as check_route_limiters,
    )
    from grelmicro.http._ratelimit import spend as spend
    from grelmicro.http._ratelimit import state_on as state_on
    from grelmicro.http._requirement import Requirement as Requirement
    from grelmicro.http._requirement import (
        declare_from_request as declare_from_request,
    )
    from grelmicro.http._requirement import declare_optional as declare_optional
    from grelmicro.http._requirement import requirement_for as requirement_for
    from grelmicro.http._response_cache import declare_cached as declare_cached
    from grelmicro.integrations._fastapi_auth import (
        Anonymous,
        Authenticated,
        Claims,
        CurrentPrincipal,
        CurrentToken,
        OptionalPrincipal,
    )
    from grelmicro.integrations._fastapi_cache import CachedResponse
    from grelmicro.integrations._fastapi_conditional import (
        Conditional,
        ConditionalRequest,
        ConditionalRequired,
    )
    from grelmicro.integrations._fastapi_metrics import metrics_router
    from grelmicro.integrations._fastapi_ratelimit import RateLimited
    from grelmicro.integrations._request_telemetry import (
        exceptions_on_spans as exceptions_on_spans,
    )
    from grelmicro.integrations._request_telemetry import excluding as excluding
    from grelmicro.integrations._request_telemetry import (
        normal_close as normal_close,
    )
    from grelmicro.integrations._route_gate import (
        declarations_of as declarations_of,
    )
    from grelmicro.integrations._route_gate import gate_routes as gate_routes
    from grelmicro.integrations._route_gate import (
        shadowed_routes as shadowed_routes,
    )
    from grelmicro.integrations._starlette_errors import error_response
    from grelmicro.integrations.starlette import is_bound
    from grelmicro.metrics import Metrics as Metrics
    from grelmicro.metrics._endpoints import (
        PROMETHEUS_MEDIA_TYPE as PROMETHEUS_MEDIA_TYPE,
    )
    from grelmicro.metrics._endpoints import (
        render_prometheus as render_prometheus,
    )
    from grelmicro.resilience import RateLimiter as RateLimiter
    from grelmicro.resilience import (
        RateLimitExceededError as RateLimitExceededError,
    )
    from grelmicro.security import JWTClaims as JWTClaims
    from grelmicro.security import Principal as Principal
    from grelmicro.security import TrustedProxies as TrustedProxies
    from grelmicro.security import VerifiedToken as VerifiedToken
    from grelmicro.trace import Trace as Trace
    from grelmicro.trace._autoinstrument import is_selected as is_selected

__all__ = [
    "Anonymous",
    "Authenticated",
    "CachedResponse",
    "CheckResultResponse",
    "Claims",
    "Conditional",
    "ConditionalRequest",
    "ConditionalRequired",
    "CurrentPrincipal",
    "CurrentToken",
    "HealthzResponse",
    "OptionalPrincipal",
    "RateLimited",
    "error_response",
    "health_router",
    "install",
    "install_error_responses",
    "install_middleware",
    "install_route_gate",
    "is_bound",
    "metrics_router",
    "route_declarations",
]

_LAZY: dict[str, str] = {
    "Anonymous": "grelmicro.integrations._fastapi_auth",
    "Authenticated": "grelmicro.integrations._fastapi_auth",
    "CachedResponse": "grelmicro.integrations._fastapi_cache",
    "Claims": "grelmicro.integrations._fastapi_auth",
    "Conditional": "grelmicro.integrations._fastapi_conditional",
    "ConditionalRequest": "grelmicro.integrations._fastapi_conditional",
    "ConditionalRequired": "grelmicro.integrations._fastapi_conditional",
    "CurrentPrincipal": "grelmicro.integrations._fastapi_auth",
    "CurrentToken": "grelmicro.integrations._fastapi_auth",
    "OptionalPrincipal": "grelmicro.integrations._fastapi_auth",
    "RateLimited": "grelmicro.integrations._fastapi_ratelimit",
    "error_response": "grelmicro.integrations._starlette_errors",
    "is_bound": "grelmicro.integrations.starlette",
    "metrics_router": "grelmicro.integrations._fastapi_metrics",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the module.

    A name the module served in 0.43 without listing it loads from its home
    on each access and warns that it moves out in 0.45.0.
    """
    module = _LAZY.get(name)
    if module is None:
        if not name.startswith("_"):
            from grelmicro._deprecation import FASTAPI_MOVED  # noqa: PLC0415

            return FASTAPI_MOVED.load(name)
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})


def install(
    app: Annotated[
        FastAPI,
        Doc("The FastAPI application to wire."),
    ],
    micro: Annotated[
        Grelmicro,
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
    """Wire `micro` into a FastAPI app.

    The Starlette wiring, plus FastAPI's request telemetry exported through
    the `Trace` and `Metrics` components. The access record and the
    security event of a request name the route FastAPI's request span
    names.

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """
    _wire_starlette(app, micro, ambient=ambient)
    _wire_route_reading_starlette(app, _route_reader)
    _wire_telemetry(app, micro)


def _route_reader(app: FastAPI) -> Callable[..., str | None]:
    """Return the route reader of `app`, naming the route FastAPI's request span names.

    The template starts with the root path the outermost app was reached
    under, then the route FastAPI's router selected, with the prefix of
    every router it was included by. A mount reads as `{path}` under its
    template, unless a FastAPI app or router serves it, whose route then
    follows the mount. Under a FastAPI app this one is mounted in, the
    route reads from that app's router, which names the span. A `Host`
    names no route. A request a router redirects to add or drop a trailing
    slash reads the route it is redirected to, and a frontend route reads
    as `{path}` under the frontend's path. A request no router reached
    names no route, unless `reach` asks for the route its path would reach.

    Raises:
        RuntimeError: When FastAPI no longer has a part of its router the
            reader reads.
    """
    held = weakref.ref(app.router)
    _fastapi.require(app.router)

    def route(
        scope: Scope,
        root_path: str,
        path: str,
        status: int | None,  # noqa: ARG001
        /,
        *,
        reach: bool = False,
    ) -> str | None:
        root = scope.get("app_root_path", root_path)
        outer = scope.get("router")
        start = router = held()
        if outer is None:
            if not reach:
                return None
        elif outer is not router and isinstance(outer, _fastapi.ROUTERS):
            start, root_path = outer, root
        else:
            matched = scope.get("route")
            if (
                _fastapi.is_route(matched)
                and scope.get("root_path", "") == root_path
            ):
                return root.rstrip("/") + _fastapi.template_of(scope, matched)
        return _fastapi.selected_route(
            start,
            {**scope, "path": path, "root_path": root_path, "path_params": {}},
            root.rstrip("/"),
        )

    return route


def install_error_responses(
    app: Annotated[
        FastAPI,
        Doc("The FastAPI application to wire."),
    ],
    errors: Annotated[
        "ErrorResponses",  # noqa: UP037
        Doc("The registered component that renders each rejection."),
    ],
) -> None:
    """Answer every error in the registered format, schema included.

    The Starlette handlers, plus the OpenAPI rewriting: FastAPI describes a
    generated `422` with its own model, which is no longer what those
    operations answer with.

    Read more in the [Error Responses](../http/errors.md) docs.
    """
    _install_error_responses_starlette(app, errors)
    _document_error_responses(app, errors)


def install_middleware(
    app: Annotated[
        FastAPI,
        Doc("The FastAPI application to wire."),
    ],
    components: Annotated[
        Sequence[Any],
        Doc("The registered components that carry an ASGI middleware."),
    ],
) -> None:
    """Add each component's ASGI middleware and describe it in the schema.

    The Starlette wiring reads route rules and adds the middleware. Each
    component carrying `_document_openapi` is handed the schema when FastAPI
    builds it, with the route declarations and the registered error format,
    so a route added after this call is described too. A component without
    it is added silently.
    """
    _install_middleware_starlette(app, components)
    _document_components(app, components)


def install_route_gate(
    app: Annotated[
        FastAPI,
        Doc("The FastAPI application whose routes to gate."),
    ],
    gate: Annotated[
        Gate,
        Doc(
            "Returns the app to dispatch to in place of a route, given it "
            "and the route's declarations, refusing a declaration that "
            "cannot hold."
        ),
    ],
) -> None:
    """Gate every route FastAPI dispatches to, before it reads the request.

    Each route is gated as it is dispatched: directly, and under each
    router that includes it, with what that include adds. So a request the
    route refuses is answered before its body is read. Websocket routes,
    frontend routes, and the routes of a mounted app or router are gated
    too, and a mount holding no anonymous route refuses a request without
    a credential at its door. A route added later is gated as it lands.

    `micro.install(app)` calls this when `AuthenticatedRequests` is
    registered, with the gate that component builds, and otherwise when
    `CachedResponses`, `IdempotentRequests` or `ConditionalRequests` is,
    with a gate that refuses nothing and runs them at the route.

    Read more in the [Plugins](../architecture/plugins.md#declare-the-routes)
    docs.

    Raises:
        RuntimeError: If FastAPI's router lacks what the gates rely on,
            naming it.
        TypeError: If a declaration's `cache` is neither a boolean nor a
            `timedelta`.
        ValueError: If a declaration cannot hold, naming its route.
    """
    from grelmicro.integrations._route_gate import gate_routes  # noqa: PLC0415

    gate_routes(app, gate)


def health_endpoints(
    app: Annotated[
        FastAPI,
        Doc("The FastAPI application whose health endpoints to list."),
    ],
) -> Iterator[HealthEndpoint]:
    """Yield the health endpoints the app serves, included routers too."""
    return health_endpoints_in(app.routes)


def route_declarations(
    app: Annotated[
        FastAPI,
        Doc("The FastAPI application whose routes to list."),
    ],
) -> list[RouteDeclaration]:
    """Return what every route of the app requires, as its gate reads it.

    One declaration per route, and one per include of a router's route. A
    route declaring `Anonymous()` is anonymous, and its scopes are the ones
    every `Security` around `Authenticated`, `CurrentPrincipal`, `Claims` or
    `CurrentToken` names. Any other dependency is a check of its own, except
    `CachedResponse()` and `OptionalPrincipal`. A `CachedResponse()` on the
    route is declared as it is, so one beside a check of its own fails
    install. One on a router caches the reads under it that run no check of
    their own.
    """
    from grelmicro.integrations._route_gate import (  # noqa: PLC0415
        declarations_of,
    )

    return declarations_of(app)


_front_middleware = _starlette_front_middleware
"""The middleware the app runs in front of its router, as Starlette lists it."""


def _shadowed_routes(app: FastAPI) -> frozenset[tuple[str, str]]:
    """Return each anonymous route another route declared before it serves.

    By path and method, as the route gate reads the router. Reports and
    the schema read it.
    """
    from grelmicro.integrations._route_gate import (  # noqa: PLC0415
        shadowed_routes,
    )

    return shadowed_routes(app)


def _wire_telemetry(app: FastAPI, micro: Grelmicro) -> None:
    """Hand FastAPI's request telemetry to grelmicro's export.

    FastAPI records the request spans and the HTTP server metrics through
    the global providers. When a `Trace` or `Metrics` component exports
    them, FastAPI stops setting up exporters of its own from the
    environment. A `Trace` that is off, or whose `instrument` leaves out
    `fastapi`, turns the request spans off and keeps the metrics.
    """
    settings = telemetry_of(app)
    if app in _WIRED:
        return
    _WIRED.add(app)
    kinds = {getattr(component, "kind", None) for component in micro.components}
    if "trace" not in kinds and "metrics" not in kinds:
        return
    from grelmicro.integrations._request_telemetry import (  # noqa: PLC0415
        exceptions_on_spans,
        excluding,
    )
    from grelmicro.trace._autoinstrument import is_selected  # noqa: PLC0415
    from grelmicro.trace._component import Trace  # noqa: PLC0415

    settings["auto_configure"] = False
    settings["exclude"] = excluding("fastapi", settings["exclude"])
    trace = next((c for c in micro.components if isinstance(c, Trace)), None)
    if trace is None:
        return
    if not (trace.active and is_selected("fastapi", trace.instrument)):
        settings["tracing"] = False
    elif settings["tracing"] and exceptions_on_spans():
        entry = Middleware(_ExceptionEvents)
        app.user_middleware.insert(0, entry)
        _keep_watching_outside(app, [entry])


_WIRED: Final[weakref.WeakSet[FastAPI]] = weakref.WeakSet()
"""Apps whose telemetry is wired. The first `Grelmicro` installed wires it."""


class _ExceptionEvents:
    """Record an exception the app raised as an event on the request span.

    Runs outside every middleware the app added and inside the server
    error handler, while the request span is current. A WebSocket closed
    normally records nothing.
    """

    def __init__(self, app: ASGIApp) -> None:
        from grelmicro.integrations._request_telemetry import (  # noqa: PLC0415
            normal_close,
        )

        self.app = app
        self._normal_close = normal_close

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            if not self._normal_close(exc):
                _otel_trace.get_current_span().record_exception(exc)
            raise


def _document_components(app: FastAPI, components: Sequence[Any]) -> None:
    """Hand the schema FastAPI builds to each component that describes itself.

    Each schema is edited once. FastAPI caches it and hands back the same
    object, so serving it again edits nothing, and a schema built anew is
    edited anew.
    """
    from grelmicro.http._openapi import describing  # noqa: PLC0415

    documenting = describing(components)
    if not documenting:
        return
    from grelmicro.http._authentication import (  # noqa: PLC0415
        describe_app_schema,
    )

    original = app.openapi
    edited: list[dict[str, Any]] = []

    def openapi() -> dict[str, Any]:
        schema = original()
        if edited and edited[0] is schema:
            return schema
        describe_app_schema(app, schema, documenting)
        # One slot, so serving the schema per request holds one reference.
        edited[:] = [schema]
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    # Drop a schema built before this call, which would otherwise be served
    # from the cache without the descriptions.
    app.openapi_schema = None


def _document_error_responses(
    app: FastAPI,
    errors: "ErrorResponses",  # noqa: UP037
) -> None:
    """Republish the schema's error responses in the format now installed.

    FastAPI generates a `422` for every operation that validates, pointing
    at its own `HTTPValidationError`. That is no longer the body those
    operations answer with, so a generated client would decode the wrong
    shape. The entry is rewritten to the registered media type and model.

    A no-op on a plain Starlette app, which builds no schema.
    """
    from grelmicro.http._openapi import add_error_schema  # noqa: PLC0415

    original = app.openapi
    applied: list[bool] = []

    def openapi() -> dict[str, Any]:
        # A handler registered after `install` takes the error back, so the
        # schema has to follow. Deciding here rather than at install keeps
        # the promise that the order of the two does not matter.
        renders = _renders_validation(app)
        if applied and applied[0] != renders:
            # The cached schema was built under the other answer, and the
            # rewrite mutated it in place. Drop it and build again.
            app.openapi_schema = None
        # One slot, not a log: `/openapi.json` is served per request, and a
        # growing list would be one entry per request for the process life.
        applied[:] = [renders]
        schema = original()
        if not renders:
            return schema
        targets = [
            operation
            for _, operation in _operations(schema)
            if _has_generated_validation(operation)
        ]
        if not targets:
            # Nothing answers with the model, so publishing it would leave
            # the schema carrying a component nothing points at.
            return schema
        ref = add_error_schema(schema, errors.model)
        if not ref:
            # Both names are taken by the app's own models. FastAPI's own
            # entry is a better answer than one pointing at nothing, and
            # its models have to stay for that entry to resolve.
            return schema
        for operation in targets:
            _rewrite_validation_response(operation, ref, errors.media_type)
        _drop_unreferenced_validation_schemas(schema)
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    # Drop a schema built before this call, which would otherwise be served
    # from the cache describing the shape the app no longer answers with.
    app.openapi_schema = None


def _renders_validation(app: FastAPI) -> bool:
    """Return whether grelmicro is still the one answering a bad request.

    Compared by identity against the handler `install` registered. A name
    comparison would call an app's own handler ours the moment someone
    named theirs `validation_error`, which is the obvious name for it.
    """
    try:
        from fastapi.exceptions import (  # noqa: PLC0415
            RequestValidationError,
        )
    except ImportError:  # pragma: no cover
        return False
    ours = getattr(app.state, "grelmicro_validation_handler", None)
    return (
        ours is not None
        and app.exception_handlers.get(RequestValidationError) is ours
    )


def _has_generated_validation(operation: dict[str, Any]) -> bool:
    """Return whether this operation still carries FastAPI's own `422`."""
    generated = (
        operation.get("responses", {})
        .get(str(HTTP_422_UNPROCESSABLE_CONTENT), {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    return generated.get("$ref") == _FASTAPI_VALIDATION_REF


def _rewrite_validation_response(
    operation: dict[str, Any], ref: str, media_type: str
) -> None:
    """Point one operation's generated validation response at the new body.

    Only called for an operation `_has_generated_validation` already
    picked, so there is nothing left to check here.
    """
    operation["responses"][str(HTTP_422_UNPROCESSABLE_CONTENT)]["content"] = {
        media_type: {"schema": {"$ref": ref}}
    }


_FASTAPI_VALIDATION_REF = "#/components/schemas/HTTPValidationError"
"""What FastAPI points a generated validation response at."""


_FASTAPI_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")
"""What FastAPI publishes for a validation response it generated."""


def _drop_unreferenced_validation_schemas(schema: dict[str, Any]) -> None:
    """Remove FastAPI's validation models once nothing points at them.

    Rewriting every generated `422` leaves them behind, and a schema
    carrying a model no response uses reads as though some operation still
    answers with it. Only these two are considered, and only while nothing
    outside them refers to them: an operation that declared its own `422`
    keeps FastAPI's entry, and with it the models.

    Called only after every generated `422` was rewritten, so a candidate
    that is still referenced is one the app itself points at.
    """
    from grelmicro.http._openapi import referenced  # noqa: PLC0415

    schemas = schema.get("components", {}).get("schemas", {})
    candidates = [
        name for name in _FASTAPI_VALIDATION_SCHEMAS if name in schemas
    ]
    elsewhere = {
        name: definition
        for name, definition in schemas.items()
        if name not in _FASTAPI_VALIDATION_SCHEMAS
    }
    # Everything that can point at a candidate without being one: the
    # operations, the webhooks, and the components that are not candidates.
    reachable = referenced(
        {section: schema.get(section, {}) for section in ("paths", "webhooks")}
    ) | referenced(elsewhere)
    # A surviving model keeps what it points at: `HTTPValidationError`
    # holds a list of `ValidationError`, and an app that references the
    # first itself would otherwise be left with a `$ref` to nothing.
    for name in candidates:
        if name in reachable:
            reachable |= referenced({name: schemas[name]})
    for name in candidates:
        if name not in reachable:
            del schemas[name]


def _operations(
    schema: dict[str, Any],
    methods: Collection[str] | None = None,
    sections: Collection[str] = ("paths", "webhooks"),
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return each operation in the schema, paired with its path item.

    Both `paths` and `webhooks` hold them, and a webhook left out of the
    `$ref` walk keeps a reference to a component the walk then deletes.
    Which sections matter depends on the caller: a webhook describes a
    request the app sends, so nothing a middleware does to incoming
    requests belongs on one.

    Every non-operation key of a path item (`parameters`, `servers`,
    `summary`, `$ref`) holds a list or a string, so a mapping under a method
    key is an operation. `methods` narrows to the ones a middleware covers.
    """
    return [
        (path_item, operation)
        for section in sections
        for path_item in schema.get(section, {}).values()
        for method, operation in path_item.items()
        if isinstance(operation, dict)
        and (methods is None or method.lower() in methods)
    ]


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
    include_in_schema: Annotated[
        bool,
        Doc(
            "Whether the endpoints appear in the OpenAPI schema:\n\n"
            "- ``False`` (default): served, but absent from "
            "``/openapi.json`` and the docs pages.\n"
            "- ``True``: documented like any other route, with the "
            "``/healthz`` response model and the ``503`` response.\n\n"
            "The endpoints answer the same either way. This decides "
            "what the schema publishes, not what is reachable."
        ),
    ] = False,
    show_details: Annotated[
        bool | Depends,
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
        list[Depends] | None,
        Doc(
            "FastAPI dependencies applied to ``/healthz``. A failing "
            "dependency blocks the entire endpoint (``401``/``403``). "
            "Use to hide ``/healthz`` from the public while leaving "
            "``/livez`` and ``/readyz`` open to orchestrators and "
            "load balancers. Independent of ``show_details``."
        ),
    ] = None,
) -> APIRouter:
    """Create a FastAPI router with health check endpoints.

    Provides three endpoints:

    - ``GET/HEAD {prefix}/livez``: Liveness probe. Runs no check per
      request. ``503`` while a liveness check fails, else ``200``, with
      an empty body.
    - ``GET/HEAD {prefix}/readyz``: Readiness probe. Runs critical
      checkers only. Returns ``200`` or ``503`` with an empty body.
    - ``GET/HEAD {prefix}/healthz``: Aggregate JSON report.

    All responses set ``Cache-Control: no-store``. None of them appear in
    the OpenAPI schema unless ``include_in_schema=True``.

    Raises:
        DependencyNotFoundError: If ``fastapi`` is not installed.
        TypeError: If ``show_details`` is neither a bool nor a
            ``Depends(...)`` value.
    """
    try:
        from fastapi import Query  # noqa: PLC0415
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

    def _resolve_component() -> HealthChecks:
        return component or Grelmicro.current().get("health", "default")

    show_details_dep = _resolve_show_details_dep(show_details)

    router = APIRouter(
        prefix=prefix, tags=["health"], include_in_schema=include_in_schema
    )
    healthz_deps = list(healthz_dependencies or ())

    @router.get(
        "/livez",
        status_code=HTTP_200_OK,
        response_class=Response,
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "A liveness check failed its last round.",
            },
        },
    )
    @router.head("/livez", include_in_schema=False)
    async def livez() -> Response:
        """Liveness probe: ``503`` while a liveness check fails, else ``200``."""
        try:
            alive = _resolve_component().is_alive
        except NoActiveAppError, ComponentNotRegisteredError:
            alive = True
        code = HTTP_200_OK if alive else HTTP_503_SERVICE_UNAVAILABLE
        return Response(status_code=code, headers=NO_STORE_HEADERS)

    @router.get(
        "/readyz",
        status_code=HTTP_200_OK,
        response_class=Response,
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
            exclude=parse_exclude(exclude),
        )
        return Response(
            status_code=status_code(report["status"]),
            headers=NO_STORE_HEADERS,
        )

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
        include_details: Annotated[bool, _Depends(show_details_dep)],
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
            exclude=parse_exclude(exclude),
        )
        return Response(
            content=report_body(
                report["checks"], report["status"], details=include_details
            ),
            status_code=status_code(report["status"]),
            media_type=JSON_MEDIA_TYPE,
            headers=NO_STORE_HEADERS,
        )

    for route in router.routes:
        mark_health_endpoint(getattr(route, "endpoint", None), component)
    return router


def _resolve_show_details_dep(show_details: Any) -> Callable[..., Any]:  # noqa: ANN401
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
