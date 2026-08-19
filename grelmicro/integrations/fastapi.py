"""FastAPI integration: the Starlette wiring plus what only FastAPI has.

The binding, the error responses, and the idempotency middleware are pure
ASGI and live in `grelmicro.integrations.starlette`. This module adds the
OpenAPI schema and the health router, and re-exports what a FastAPI app
uses so one import covers it.
"""

import logging
from typing import TYPE_CHECKING, Annotated, Any, cast

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.health._checks import HealthChecks
from grelmicro.health._models import CheckResult, HealthStatus
from grelmicro.http import ErrorResponses, ProblemDetail
from grelmicro.http._openapi import add_error_schema, referenced
from grelmicro.http._problem import PROBLEM_MEDIA_TYPE
from grelmicro.integrations.starlette import (
    _MAX_KEY_LENGTH,
    HTTP_422_UNPROCESSABLE_CONTENT,
    GrelmicroMiddleware,
    IdempotencyMiddleware,
    StoredResponse,
    error_response,
    is_bound,
)
from grelmicro.integrations.starlette import install as _install_starlette
from grelmicro.integrations.starlette import (
    install_error_responses as _install_error_responses_starlette,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from fastapi import APIRouter, FastAPI
    from fastapi.params import Depends

    from grelmicro import Grelmicro
    from grelmicro.trace._component import Trace

__all__ = [
    "CheckResultResponse",
    "GrelmicroMiddleware",
    "HealthzResponse",
    "IdempotencyMiddleware",
    "StoredResponse",
    "document_idempotency",
    "error_response",
    "health_router",
    "install",
    "install_error_responses",
    "is_bound",
]

_logger = logging.getLogger(__name__)


def install(
    app: Annotated[
        "FastAPI",
        Doc("The FastAPI application to wire."),
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
    """Wire `micro` into a FastAPI app.

    The Starlette wiring, plus OpenTelemetry auto-instrumentation when a
    `Trace` component asks for it.

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """
    _install_starlette(app, micro, ambient=ambient)
    _instrument_app(app, micro)


def install_error_responses(
    app: Annotated[
        "FastAPI",
        Doc("The FastAPI application to wire."),
    ],
    errors: Annotated[
        ErrorResponses,
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


def _instrument_app(app: "FastAPI", micro: "Grelmicro") -> None:
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
    next time it is built, so routes added afterwards are covered too, and
    an `ErrorResponses` registered later is still the format published.

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
        # Read when the schema is built rather than when this is called, so
        # the order of `document_idempotency` and `micro.install` cannot
        # publish a format the app does not answer in.
        errors = getattr(app.state, "grelmicro_error_responses", None)
        schema = original()
        _annotate_schema(
            schema,
            options,
            PROBLEM_MEDIA_TYPE if errors is None else errors.media_type,
            ProblemDetail if errors is None else errors.model,
        )
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


def _annotate_schema(
    schema: dict[str, Any],
    options: dict[str, Any],
    media_type: str,
    model: type[BaseModel],
) -> None:
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
        # Only what the middleware serves. A webhook is a request the app
        # sends, and no `Idempotency-Key` of ours reaches it.
        for path_item, operation in _operations(schema, methods, ("paths",))
    ]
    if not covered:
        return
    ref = add_error_schema(schema, model)
    for path_item, operation in covered:
        _add_parameter(operation, path_item, parameter)
        for status, description in responses.items():
            _merge_response(operation, status, description, ref, media_type)


def _document_error_responses(app: "FastAPI", errors: ErrorResponses) -> None:
    """Republish the schema's error responses in the format now installed.

    FastAPI generates a `422` for every operation that validates, pointing
    at its own `HTTPValidationError`. That is no longer the body those
    operations answer with, so a generated client would decode the wrong
    shape. The entry is rewritten to the registered media type and model.

    A no-op on a plain Starlette app, which builds no schema.
    """
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


def _renders_validation(app: "FastAPI") -> bool:
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
    methods: "Collection[str] | None" = None,
    sections: "Collection[str]" = ("paths", "webhooks"),
) -> "list[tuple[dict[str, Any], dict[str, Any]]]":
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
    operation: dict[str, Any],
    status: str,
    description: str,
    ref: str,
    media_type: str,
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
            media_type, {"schema": {"$ref": ref}}
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
