"""FastAPI integration: the Starlette wiring plus what only FastAPI has.

The lifespan, the binding, and the error responses are pure ASGI and live in
`grelmicro.integrations.starlette`. This module adds what only FastAPI has,
the OpenAPI schema and the health router.
"""

import logging
from typing import TYPE_CHECKING, Annotated, Any, cast

try:
    # The dependency declares its headers, so these are needed where the
    # class is defined rather than where it is called. Every other door
    # into this module imports FastAPI when it runs, because the module
    # has to import without it.
    from fastapi import Depends as _Depends
    from fastapi import Header as _Header

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - the reimport test walks this
    HAS_FASTAPI = False

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from typing_extensions import Doc

from grelmicro._guards import is_class, is_subclass
from grelmicro._json import json_dumps_bytes
from grelmicro.health._checks import HealthChecks
from grelmicro.health._models import CheckResult, HealthStatus
from grelmicro.http import (
    ConditionalRequestsMiddleware,
    ErrorResponses,
    IdempotencyMiddleware,
    PreconditionRequiredError,
    ProblemDetail,
    check_freshness,
)
from grelmicro.http._conditional import _UNSET as _UNSET_VERSION
from grelmicro.http._conditional import _check_sent_precondition
from grelmicro.http._idempotency import _KEY_PATTERN, _MAX_KEY_LENGTH
from grelmicro.http._openapi import add_error_schema, referenced
from grelmicro.http._paths import selects
from grelmicro.http._problem import PROBLEM_MEDIA_TYPE
from grelmicro.integrations.starlette import (
    HTTP_422_UNPROCESSABLE_CONTENT,
    error_response,
    is_bound,
)
from grelmicro.integrations.starlette import install as _install_starlette
from grelmicro.integrations.starlette import (
    install_error_responses as _install_error_responses_starlette,
)
from grelmicro.integrations.starlette import (
    install_middleware as _install_middleware_starlette,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from fastapi import APIRouter, FastAPI
    from fastapi.params import Depends

    from grelmicro import Grelmicro
    from grelmicro.idempotency import Idempotency
    from grelmicro.trace._component import Trace

__all__ = [
    "CheckResultResponse",
    "Conditional",
    "ConditionalRequest",
    "ConditionalRequired",
    "HealthzResponse",
    "document_conditional_requests",
    "document_idempotency",
    "error_response",
    "health_router",
    "install",
    "install_error_responses",
    "install_middleware",
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


def install_middleware(
    app: Annotated[
        "FastAPI",
        Doc("The FastAPI application to wire."),
    ],
    components: Annotated[
        "Sequence[Any]",
        Doc("The registered components that carry an ASGI middleware."),
    ],
) -> None:
    """Add each component's ASGI middleware and describe it in the schema.

    The Starlette wiring, plus the OpenAPI part: a middleware runs outside
    the routing layer, so nothing it does reaches the generated schema
    unless something writes it there. A component that carries
    `document_openapi(app)` is asked to, and one that does not is added
    silently.
    """
    _install_middleware_starlette(app, components)
    for component in components:
        document = getattr(component, "document_openapi", None)
        if document is not None:
            document(app)


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
    *,
    idempotency: Annotated[
        "Idempotency[Any] | None",
        Doc(
            "Describe only the middleware storing through this "
            "`Idempotency`. Defaults to every one the app carries."
        ),
    ] = None,
) -> None:
    """Describe the installed `IdempotencyMiddleware` in the OpenAPI schema.

    A middleware runs outside the routing layer, so nothing it does reaches
    the generated schema and a client built from that schema never learns
    the header exists. This reads the installed middleware and annotates
    every operation it covers with the key header parameter, the replay
    header on the responses that can carry it, and the responses the
    middleware itself can return.

    An app running two sets of rules has each described under its own
    paths and its own two header names. Pass `idempotency` to describe one
    of them and leave the other out of the schema. An app that wired the
    middleware by hand stores through none of the registered components,
    and every installed middleware is described.

    ```python
    from grelmicro.http import IdempotencyMiddleware
    from grelmicro.integrations.fastapi import document_idempotency

    app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
    micro.install(app)
    document_idempotency(app)
    ```

    Registering `IdempotentRequests()` calls this for you, so a direct call
    is for a middleware added by hand.

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
    _require_fastapi(app, "document_idempotency")
    _idempotency_options(app)
    original = app.openapi

    def openapi() -> dict[str, Any]:
        # Read when the schema is built rather than when this is called, so
        # the order of `document_idempotency` and `micro.install` cannot
        # publish a format the app does not answer in, and so a middleware
        # added after this call is described too.
        errors = getattr(app.state, "grelmicro_error_responses", None)
        schema = original()
        installed = list(enumerate(_idempotency_options(app)))
        # A component names the middleware it registered, so a second set
        # of rules stays out of the schema. Naming one the app does not
        # carry means the app wired the middleware itself, which serves
        # the component's rules, so every installed one is described.
        named = [
            entry
            for entry in installed
            if idempotency is None or entry[1]["idempotency"] is idempotency
        ]
        described, refusals = _annotation_state(app, schema)
        for index, options in named or installed:
            if index in described:
                continue
            described.add(index)
            _annotate_schema(
                schema,
                options,
                PROBLEM_MEDIA_TYPE if errors is None else errors.media_type,
                ProblemDetail if errors is None else errors.model,
                refusals,
            )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    # Drop a schema built before this call, which would otherwise be
    # served from the cache without the annotations.
    app.openapi_schema = None


class ConditionalRequest:
    """The conditional guards, as a dependency FastAPI injects.

    The same two calls as `check_freshness` and `check_precondition`, for a
    handler that would rather have them injected than imported:

    ```python
    from grelmicro.integrations.fastapi import Conditional


    @app.patch("/carts/{cart_id}")
    async def update(cart_id: int, conditional: Conditional) -> Cart:
        cart = await load(cart_id)
        conditional.check(cart.version)
        return await save(cart)
    ```

    It is the same implementation underneath, so a service can use
    whichever reads better. The plain functions work on Starlette and
    Litestar too, where there is no dependency injection to hang this on.

    `check` reads the headers this was handed, so a route that injects it
    is guarded whether or not `ConditionalRequests()` is registered.
    `fresh` needs the component: recording a version is how the middleware
    knows what tag to put on the response, and without one there is
    nothing to record it in, so it raises `OutOfContextError`.

    Declaring it puts `If-Match` and `If-None-Match` in the schema for
    that operation alone, so Swagger offers the fields exactly where a
    handler reads them, whatever the path-based rules say.
    """

    def __init__(
        self,
        if_match: Annotated[
            "str | None",
            Doc("What the client sent in `If-Match`, if anything."),
        ] = None,
        if_none_match: Annotated[
            "str | None",
            Doc("What the client sent in `If-None-Match`, if anything."),
        ] = None,
    ) -> None:
        """Take what the client sent, so a handler can read it raw too."""
        self.if_match = if_match
        self.if_none_match = if_none_match

    def check(
        self,
        version: Annotated[
            object,
            Doc("What identifies the version the resource carries now."),
        ] = _UNSET_VERSION,
        *,
        etag: Annotated[
            str | None,
            Doc("An entity tag that is already one, quotes included."),
        ] = None,
        require: Annotated[
            bool,
            Doc("Answer `428` when the request carries no precondition."),
        ] = True,
    ) -> None:
        """Refuse a write whose precondition no longer holds.

        Raises:
            PreconditionFailedError: If the entity tag is not current.
            PreconditionRequiredError: If `require` and none was sent.
        """
        # From the headers this was handed, not from the request scope, so
        # a route that injects it answers the same whether or not
        # `ConditionalRequests()` is registered.
        _check_sent_precondition(
            self.if_match,
            self.if_none_match,
            version,
            etag=etag,
            require=require,
        )

    def fresh(
        self,
        version: Annotated[
            object,
            Doc("What identifies the version the resource carries now."),
        ] = _UNSET_VERSION,
        *,
        etag: Annotated[
            str | None,
            Doc("An entity tag that is already one, quotes included."),
        ] = None,
    ) -> bool:
        """Answer this read with an entity tag, and say whether it changed."""
        return check_freshness(version, etag=etag)


def document_conditional_requests(
    app: Annotated[
        "FastAPI",
        Doc("The app carrying a `ConditionalRequestsMiddleware` to document."),
    ],
) -> None:
    """Describe the conditional headers in the OpenAPI schema.

    A middleware runs outside the routing layer, so nothing it does reaches
    the generated schema, and Swagger shows no field for the header a
    client has to send. This annotates every operation the middleware
    covers:

    - A read gains `If-None-Match` and the `304` it can answer.
    - A write gains `If-Match`, the `412` a stale one gets, and the `428` a
      missing one gets. The header is marked required on a method named in
      `require_precondition`.

    ```python
    from grelmicro.integrations.fastapi import document_conditional_requests

    app.add_middleware(ConditionalRequestsMiddleware)
    document_conditional_requests(app)
    ```

    Registering `ConditionalRequests()` calls this for you, so a direct
    call is for a middleware added by hand. Pass `openapi=False` to the
    component to leave the schema alone.

    Raises:
        DependencyNotFoundError: If `fastapi` is not installed.
        TypeError: If `app` is not a `FastAPI` app, or carries no
            `ConditionalRequestsMiddleware`.
    """
    _require_fastapi(app, "document_conditional_requests")
    options = _middleware_options(
        app,
        ConditionalRequestsMiddleware,
        "document_conditional_requests() found no "
        "ConditionalRequestsMiddleware on the app. Add it with "
        "app.add_middleware(ConditionalRequestsMiddleware) first.",
    )
    original = app.openapi

    def openapi() -> dict[str, Any]:
        errors = getattr(app.state, "grelmicro_error_responses", None)
        schema = original()
        _annotate_conditional(
            schema,
            options,
            PROBLEM_MEDIA_TYPE if errors is None else errors.media_type,
            ProblemDetail if errors is None else errors.model,
            _required_routes(app),
        )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    app.openapi_schema = None


_CREATE_CASE = "Required, unless the request creates with `If-None-Match: *`."
"""What `required` alone cannot say: either header satisfies the rule."""

_IF_MATCH = "If-Match"
"""Header a client sends to say which version it is updating."""

_IF_NONE_MATCH = "If-None-Match"
"""Header a client sends to say which version it already holds."""

_READ_METHODS = ("get", "head")
"""Methods a `304` may answer."""

_WRITE_METHODS = ("put", "patch", "delete")
"""Methods a precondition guards. `POST` creates, so it has none to hold."""


def _required_routes(app: "FastAPI") -> set[tuple[str, str]]:
    """Return the `(path, method)` pairs that declared a precondition.

    A guard called inside a handler body is invisible from here, which is
    why requiring one is something a route declares rather than calls.
    """
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        # The resolved dependency tree, under FastAPI's own spelling of it.
        # `path` is only on the routes that have one.
        declared = getattr(route, "dependant", None)  # codespell:ignore
        path = getattr(route, "path", None)
        if declared is None or path is None:
            continue
        if not any(
            getattr(dependency.call, "__name__", "") == "_required_conditional"
            for dependency in declared.dependencies
        ):
            continue
        for method in getattr(route, "methods", ()):
            found.add((path, method.lower()))
    return found


def _annotate_conditional(
    schema: dict[str, Any],
    options: dict[str, Any],
    media_type: str,
    model: type[BaseModel],
    required_routes: set[tuple[str, str]],
) -> None:
    """Add the conditional headers and responses to covered operations."""
    include = tuple(options["include"])
    exclude = tuple(options["exclude"])
    required = {method.lower() for method in options["require_precondition"]}
    reads = [
        (path, path_item, operation)
        for path, path_item, operation in _paths(schema, _READ_METHODS)
        if selects(path, include=include, exclude=exclude)
    ]
    writes = [
        (path, path_item, operation, method)
        for path, path_item, operation, method in _paths_with_method(
            schema, _WRITE_METHODS
        )
        if selects(path, include=include, exclude=exclude)
    ]
    if not reads and not writes:
        return
    ref = add_error_schema(schema, model)

    for _path, path_item, operation in reads:
        _add_parameter(
            operation,
            path_item,
            {
                "name": _IF_NONE_MATCH,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": (
                    "Entity tag the client already holds, from the `ETag` "
                    "of an earlier read. The service answers `304 Not "
                    "Modified` while it still matches."
                ),
            },
        )
        _merge_response(
            operation,
            "304",
            "The entity tag still matches, so the body is not sent again.",
            "",
            media_type,
        )

    for path, path_item, operation, method in writes:
        needed = method in required or (path, method) in required_routes
        if needed:
            _mark_required(operation, _IF_MATCH)
        _add_parameter(
            operation,
            path_item,
            {
                "name": _IF_MATCH,
                "in": "header",
                "required": needed,
                "schema": {"type": "string"},
                "description": (
                    "Entity tag of the version being updated, from the "
                    "`ETag` of an earlier read. The write is refused if "
                    "the resource changed since."
                    + (f" {_CREATE_CASE}" if needed else "")
                ),
            },
        )
        _merge_response(
            operation,
            "412",
            "The entity tag in `If-Match` is not the one the resource "
            "carries now.",
            ref,
            media_type,
        )
        _merge_response(
            operation,
            "428",
            "This request must carry a precondition.",
            ref,
            media_type,
        )


def _mark_required(operation: dict[str, Any], name: str) -> None:
    """Mark a header the operation already declares as required.

    The dependency puts it there, so the annotation cannot add a second
    one, and OpenAPI forbids a duplicate anyway. The description gains
    what `required` cannot say: a schema has no way to express that one
    header or the other will do.
    """
    lowered = name.lower()
    for parameter in operation.get("parameters", ()):
        if (
            parameter.get("in") == "header"
            and str(parameter.get("name", "")).lower() == lowered
        ):
            parameter["required"] = True
            description = str(parameter.get("description", ""))
            if _CREATE_CASE not in description:
                parameter["description"] = (
                    f"{description} {_CREATE_CASE}".strip()
                )


def _paths(
    schema: dict[str, Any],
    methods: "Collection[str]",
) -> "list[tuple[str, dict[str, Any], dict[str, Any]]]":
    """Return each operation of these methods, with the path it sits on."""
    return [
        (path, path_item, operation)
        for path, path_item, operation, _method in _paths_with_method(
            schema, methods
        )
    ]


def _paths_with_method(
    schema: dict[str, Any],
    methods: "Collection[str]",
) -> "list[tuple[str, dict[str, Any], dict[str, Any], str]]":
    """Return each operation of these methods, with its path and method.

    Only `paths`: a webhook is a request the app sends, and no conditional
    header of ours reaches it.
    """
    return [
        (path, path_item, operation, method.lower())
        for path, path_item in schema.get("paths", {}).items()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and method.lower() in methods
    ]


def _require_fastapi(app: Any, caller: str) -> None:  # noqa: ANN401
    """Refuse anything but a FastAPI app, which is what builds a schema.

    Raises:
        DependencyNotFoundError: If `fastapi` is not installed.
        TypeError: If `app` is not a `FastAPI` app.
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
            f"{caller}() needs a FastAPI app, got {type(app).__name__}. "
            f"Only FastAPI builds an OpenAPI schema."
        )
        raise TypeError(msg)


def _annotation_state(
    app: "FastAPI", schema: dict[str, Any]
) -> tuple[set[int], dict[int, set[str]]]:
    """Return what this build of the schema already carries.

    FastAPI caches the schema it builds and hands back the same object,
    and two components leave two wrappers over it. The first set holds the
    middlewares already described, so each is described once by whichever
    wrapper reaches the schema first. The second holds the refusals this
    annotation added to an operation, which the app never answers itself
    and no replay ever carries.
    """
    current = getattr(app.state, "grelmicro_idempotency_state", None)
    if current is None or current[0] is not schema:
        current = (schema, set(), {})
        app.state.grelmicro_idempotency_state = current
    return cast("tuple[set[int], dict[int, set[str]]]", current[1:])


def _middleware_options(
    app: "FastAPI", middleware: type[Any], missing: str
) -> dict[str, Any]:
    """Return one installed middleware's arguments, defaults filled in.

    The first match is the last added, which is the outermost at runtime
    and answers first on the wire.

    Raises:
        TypeError: If the app carries no such middleware.
    """
    return _every_middleware_options(app, middleware, missing)[0]


def _every_middleware_options(
    app: "FastAPI", middleware: type[Any], missing: str
) -> list[dict[str, Any]]:
    """Return every installed middleware's arguments, defaults filled in.

    An app may carry two sets of rules, each with its own paths and its
    own headers, and the schema describes what each one covers.

    Raises:
        TypeError: If the app carries no such middleware.
    """
    import inspect  # noqa: PLC0415

    signature = inspect.signature(middleware)
    found = []
    for entry in app.user_middleware:
        cls = entry.cls
        if is_class(cls) and is_subclass(cls, middleware):
            # Every parameter after `app` is keyword-only, so
            # `add_middleware` can only have passed them by keyword. A
            # subclass may take keywords of its own, which say nothing
            # about what this describes.
            bound = signature.bind_partial(
                **{
                    name: value
                    for name, value in entry.kwargs.items()
                    if name in signature.parameters
                }
            )
            bound.apply_defaults()
            found.append(dict(bound.arguments))
    if not found:
        raise TypeError(missing)
    return found


def _idempotency_options(app: "FastAPI") -> list[dict[str, Any]]:
    """Return every installed middleware's arguments, defaults filled in."""
    return _every_middleware_options(
        app,
        IdempotencyMiddleware,
        "document_idempotency() found no IdempotencyMiddleware on the app. "
        "Add it with app.add_middleware(IdempotencyMiddleware, ...) first.",
    )


def _annotate_schema(
    schema: dict[str, Any],
    options: dict[str, Any],
    media_type: str,
    model: type[BaseModel],
    refusals: dict[int, set[str]],
) -> None:
    """Add the header and the middleware's responses to covered operations."""
    methods = {method.lower() for method in options["methods"]}
    header = options["key_header"]
    fingerprint_body = options["fingerprint_body"]
    parameter = {
        "name": header,
        "in": "header",
        "required": options["require_key"],
        "schema": {
            "type": "string",
            "maxLength": _MAX_KEY_LENGTH,
            "pattern": _KEY_PATTERN,
        },
        "description": (
            "Key that makes this request safe to retry. A repeat within the "
            "replay window returns the first response instead of running the "
            f"operation again. Up to {_MAX_KEY_LENGTH} printable ASCII "
            f"characters, such as a UUID."
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
        reused = str(options["reused_status"])
        description = (
            f"This `{header}` was already used with a different request "
            f"payload."
        )
        # A service answering the reuse case with `400` shares the status
        # with the missing-key case, so both descriptions have to survive.
        responses[reused] = (
            f"{responses[reused]} {description}"
            if reused in responses
            else description
        )

    include = tuple(options["include"])
    exclude = tuple(options["exclude"])
    # `_paths` reads `paths` alone: a webhook is a request the app sends,
    # and no `Idempotency-Key` of ours reaches it.
    serves = _paths(schema, methods)
    covered = [
        (path_item, operation)
        for path, path_item, operation in serves
        if selects(path, include=include, exclude=exclude)
    ]
    if not covered and include:
        # The patterns match what the request carries, which holds the
        # prefix a mount or a `root_path` adds and the schema does not. An
        # `include` written for the wire therefore selects nothing here,
        # and dropping every annotation would leave a client with no
        # header at all. Describing what `exclude` leaves is the answer
        # that is wrong in the safe direction. An `exclude` that empties
        # the selection on its own is a service naming its own routes, and
        # is followed.
        covered = [
            (path_item, operation)
            for path, path_item, operation in serves
            if selects(path, include=(), exclude=exclude)
        ]
    if not covered:
        return
    ref = add_error_schema(schema, model)
    for path_item, operation in covered:
        _add_parameter(operation, path_item, parameter)
        added = refusals.setdefault(id(operation), set())
        # Before the refusals below are merged in, and never on one an
        # earlier pass merged: a refusal is answered before the app runs,
        # so no replay ever carries it.
        _add_replay_header(operation, options["replay_header"], added)
        answered = set(operation.get("responses", {}))
        for status, description in responses.items():
            if status not in answered:
                added.add(status)
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


def _add_replay_header(
    operation: dict[str, Any], name: str, refusals: set[str]
) -> None:
    """Describe the replay marker on every response of an operation.

    The name is a service's to pick, so the schema is where a client
    author reads it. Every status the app answers is stored and replayed,
    errors included, so the marker is described on all of them. Which
    statuses those are is a property of the middleware order at runtime,
    not of the schema, so the header is described as one that may appear
    rather than one that always does. A response that declares the header
    itself, under any casing, keeps its own.
    """
    lowered = name.lower()
    for status, response in operation.get("responses", {}).items():
        if status in refusals:
            continue
        headers = response.setdefault("headers", {})
        if any(declared.lower() == lowered for declared in headers):
            continue
        headers[name] = {
            "schema": {"type": "string", "enum": ["true"]},
            "description": (
                "Sent when this response replays an earlier request that "
                "carried the same idempotency key. Absent when the "
                "operation ran."
            ),
        }


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


if TYPE_CHECKING:
    # The runtime values are `Annotated` markers FastAPI reads. A checker
    # only needs to know what the handler receives.
    Conditional = ConditionalRequest
    ConditionalRequired = ConditionalRequest
elif HAS_FASTAPI:

    def _conditional(
        if_match: Annotated[
            str | None,
            _Header(
                alias="If-Match",
                description=(
                    "Entity tag of the version being updated, from the "
                    "`ETag` of an earlier read. The write is refused if "
                    "the resource changed since."
                ),
            ),
        ] = None,
        if_none_match: Annotated[
            str | None,
            _Header(
                alias="If-None-Match",
                description=(
                    "Entity tag the client already holds. The service "
                    "answers `304 Not Modified` while it still matches."
                ),
            ),
        ] = None,
    ) -> ConditionalRequest:
        """Bind the guards to what this request carries.

        The headers are declared here rather than read from the scope, so
        the operation that injects this documents them, and Swagger offers
        the fields exactly where a handler reads them.
        """
        return ConditionalRequest(if_match, if_none_match)

    def _required_conditional(
        conditional: Annotated[ConditionalRequest, _Depends(_conditional)],
    ) -> ConditionalRequest:
        """Refuse a request that carries no precondition at all.

        Declared rather than called, so the schema marks `If-Match`
        required on this operation, and the client is answered `428`
        rather than a validation error.

        Raises:
            PreconditionRequiredError: If neither precondition header
                arrived. Answers `428`.
        """
        if conditional.if_match is None and conditional.if_none_match is None:
            raise PreconditionRequiredError
        return conditional

    Conditional = Annotated[ConditionalRequest, _Depends(_conditional)]
    ConditionalRequired = Annotated[
        ConditionalRequest, _Depends(_required_conditional)
    ]
else:  # pragma: no cover - the reimport test walks this
    Conditional = ConditionalRequest
    ConditionalRequired = ConditionalRequest
"""The conditional guards, injected.

```python
from grelmicro.integrations.fastapi import Conditional


@app.patch("/carts/{cart_id}")
async def update(cart_id: int, conditional: Conditional) -> Cart:
    cart = await load(cart_id)
    conditional.check(cart.version)
    return await save(cart)
```

A ready-made annotation, so a handler declares one word rather than
`Annotated[ConditionalRequest, _Depends(ConditionalRequest)]`.
"""
