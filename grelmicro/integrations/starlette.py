"""Starlette integration: the lifespan, the binding, and the error responses.

Everything here is pure ASGI, so it works on a plain Starlette app and on
anything built from one. `grelmicro.integrations.fastapi` builds on it and
adds what only FastAPI has, an OpenAPI schema and a health router.
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from grelmicro._app import AmbientBindingError
from grelmicro._asgi import GrelmicroMiddleware
from grelmicro.http import ErrorResponses, merge_headers
from grelmicro.http._kinds import BODYLESS_STATUSES, HANDLED

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        MutableMapping,
        Sequence,
    )

    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import Response

    from grelmicro import Grelmicro

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "error_response",
    "install",
    "install_error_responses",
    "install_middleware",
    "is_bound",
]


def install(
    app: Annotated[
        "Starlette",
        Doc("The Starlette application to wire."),
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
    """Wire `micro` into a Starlette app.

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
    if not ambient:
        micro._on_ambient_disabled()  # noqa: SLF001
    elif not is_bound(app):
        # Once. Installing twice is a wiring mistake, and a second binding
        # would set the same context variable again on every request.
        app.add_middleware(GrelmicroMiddleware, micro=micro)
        _keep_binding_outermost(app)


def install_error_responses(
    app: Annotated[
        "Starlette",
        Doc("The Starlette application to wire."),
    ],
    errors: Annotated[
        ErrorResponses,
        Doc("The registered component that renders each rejection."),
    ],
) -> None:
    """Render grelmicro rejections in a standard format on a Starlette app.

    Registers one exception handler per rejection grelmicro raises to turn a
    caller away, and reshapes the framework's own errors into the same
    format, so the whole API answers in one shape.

    Starlette looks a handler up through the raised exception's class
    hierarchy, so registering `AdmissionError` covers every rejection under
    it, including one a later release adds.

    `micro.install(app)` calls this when `ErrorResponses()` is registered.
    Call it directly only on an app that never goes through `install`.

    Read more in the [Error Responses](../http/errors.md) docs.
    """
    # Recorded so `error_response` reads the format the app actually
    # answers in, rather than assuming RFC 9457.
    app.state.grelmicro_error_responses = errors

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

    async def http_error(request: "Request", exc: Exception) -> "Response":
        """Reshape the framework's own error into the registered format."""
        from starlette.responses import Response  # noqa: PLC0415

        http_exc = cast("HTTPException", exc)
        headers = http_exc.headers or {}
        if http_exc.status_code in BODYLESS_STATUSES:
            # A `204` or a `304` carries no body by the protocol, whatever
            # format the app answers in. Starlette guards this in its own
            # handler and so must this one.
            return Response(status_code=http_exc.status_code, headers=headers)
        detail = http_exc.detail
        rendered = errors.render_status(
            http_exc.status_code,
            detail=detail if isinstance(detail, str) else None,
            extensions=_structured_detail(detail),
            instance=request.url.path,
        )
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, headers),
        )

    async def validation_error(
        request: "Request",
        exc: Exception,
    ) -> "Response":
        """Reshape a request that did not match the endpoint's shape."""
        from starlette.responses import Response  # noqa: PLC0415

        rendered = errors.render_validation(
            _field_errors(exc),
            status=HTTP_422_UNPROCESSABLE_CONTENT,
            instance=request.url.path,
        )
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    _install_framework_handlers(app, http_error, validation_error)

    for klass in HANDLED:
        # A handler the app registered first wins. Registering before
        # `install` is the natural order (build the app, register handlers,
        # wire grelmicro), and overwriting there would take a handler away
        # from an app that upgrades, without saying so.
        if klass not in app.exception_handlers:
            app.add_exception_handler(klass, handler)


def install_middleware(
    app: Annotated[
        "Starlette",
        Doc("The Starlette application to wire."),
    ],
    components: Annotated[
        "Sequence[Any]",
        Doc("The registered components that carry an ASGI middleware."),
    ],
) -> None:
    """Add the ASGI middleware each registered component asks for.

    A component that carries `asgi_middleware()` returns the middleware
    class and the arguments to build it with, and this adds it to the app.
    Registration order is wrapping order among them, so the first one
    registered answers first.

    All of them go innermost, behind whatever the app added itself, so
    authentication, CORS and the rest run before one of ours can answer a
    request on its own. They still run inside `GrelmicroMiddleware`, which
    stays outermost, so one that resolves a backend ambiently finds the app
    bound.

    `micro.install(app)` calls this with the components it found, so a
    direct call is only for an app that never goes through `install`.
    """
    from starlette.middleware import Middleware  # noqa: PLC0415

    if getattr(app, "middleware_stack", None) is not None:
        # The framework built its stack, so the list this edits is no
        # longer what serves requests. `add_middleware` refuses the same
        # call for the same reason, and silence here would look installed
        # and answer nothing.
        msg = (
            "Cannot add middleware after an application has started. Call "
            "micro.install(app) before the app serves its first request."
        )
        raise RuntimeError(msg)

    wired = {
        entry.cls
        for entry in app.user_middleware
        if isinstance(entry.cls, type)
    }
    added = [
        Middleware(middleware, **options)
        for middleware, options in (
            component.asgi_middleware() for component in components
        )
        # One is enough. An app that added it by hand placed it where it
        # wanted, and a second would store, tag and answer twice.
        if middleware not in wired
    ]
    # Innermost, behind every middleware the app added itself. One of ours
    # that answers a request without calling the app, such as an idempotent
    # replay, must never be the reason a request skipped the authentication
    # the app put in front of its handlers. `add_middleware` prepends,
    # which would do exactly that.
    app.user_middleware = _binding_first([*app.user_middleware, *added])
    for component in components:
        _answer_for(app, component)


HTTP_422_UNPROCESSABLE_CONTENT = 422
"""Status FastAPI answers a request that failed validation with."""


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


def _answer_for(app: "Starlette", component: Any) -> None:  # noqa: ANN401
    """Register a handler for what this component answers itself.

    A component that carries `handled_exceptions()` names the rejections
    its registration is the opt-in for, so a service that asked for them
    gets the status on the wire rather than a `500`. The format is read
    per request from whichever `ErrorResponses` the app registered, and is
    RFC 9457 when it registered none.

    A handler the app registered first wins, and so does the one
    `install_error_responses` registered, which renders the same way.
    """
    handled = getattr(component, "handled_exceptions", None)
    if handled is None:
        return

    async def handler(request: "Request", exc: Exception) -> "Response":
        """Render one rejection in the format the app answers in."""
        from starlette.responses import Response  # noqa: PLC0415

        errors = (
            getattr(request.app.state, "grelmicro_error_responses", None)
            or ErrorResponses()
        )
        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    for klass in handled():
        if klass not in app.exception_handlers:
            app.add_exception_handler(klass, handler)


def _binding_first(
    entries: "Sequence[Any]",
) -> "list[Any]":
    """Return the middleware entries with the binding in front.

    Everything else keeps its order, so the only thing this decides is that
    a middleware resolving a backend ambiently runs inside the request
    scope.
    """
    return [
        *(entry for entry in entries if _is_binding(entry)),
        *(entry for entry in entries if not _is_binding(entry)),
    ]


def _keep_binding_outermost(app: "Starlette") -> None:
    """Move `GrelmicroMiddleware` to the front of the stack as it is built.

    The framework builds its middleware stack once, on the first request or
    lifespan event, from `app.user_middleware`. Reordering that list at build
    time puts the binding middleware outside every other one, so a middleware
    added after `install` still runs inside the grelmicro request scope.
    """
    build = app.build_middleware_stack

    def build_middleware_stack() -> "ASGIApp":
        app.user_middleware = _binding_first(app.user_middleware)
        return build()

    app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]


def error_response(
    request: Annotated[
        "Request",
        Doc("The request being answered, which knows its app."),
    ],
    *,
    status: Annotated[int, Doc("HTTP status code of the response.")],
    detail: Annotated[
        str | None,
        Doc("Explanation of this occurrence, safe to show a client."),
    ] = None,
    extensions: Annotated[
        dict[str, Any] | None,
        Doc("Extra members to carry, where the format has room for them."),
    ] = None,
) -> "Response":
    """Answer from your own exception handler in the app's error format.

    Writing a handler of your own is how one error opts out of the shape
    grelmicro installs. This is for the other case: you want your own
    handler and the same shape as everything else.

    ```python
    from grelmicro.integrations.fastapi import error_response


    @app.exception_handler(InsufficientFunds)
    async def handle(request: Request, exc: InsufficientFunds) -> Response:
        return error_response(
            request,
            status=409,
            detail="The account does not hold enough to cover this charge.",
            extensions={"balance": exc.balance},
        )
    ```

    The format is read from the app, so a service that registered
    `ErrorResponses.tmf()` answers in TMF from here too, with no second
    place to keep in step. An app that registered nothing gets RFC 9457.
    """
    from starlette.responses import Response  # noqa: PLC0415

    errors = (
        getattr(request.app.state, "grelmicro_error_responses", None)
        or ErrorResponses()
    )
    rendered = errors.render_status(
        status,
        detail=detail,
        instance=request.url.path,
        extensions=extensions,
    )
    return Response(
        content=rendered.body,
        status_code=rendered.status,
        media_type=rendered.media_type,
        headers=rendered.headers,
    )


def _framework_errors() -> "list[tuple[type[Exception], bool]]":
    """Return the framework's own errors, paired with whether they validate.

    Registering these is what makes the whole API answer in one shape. An
    `HTTPException` a handler raises and a request that failed validation
    are errors the client caused, so they belong in the same format as a
    rejection.
    """
    from starlette.exceptions import HTTPException  # noqa: PLC0415

    errors: list[tuple[type[Exception], bool]] = [(HTTPException, False)]
    try:
        from fastapi.exceptions import (  # noqa: PLC0415
            RequestValidationError,
        )
    except ImportError:  # pragma: no cover
        # A plain Starlette app validates nothing, so there is no such
        # error to reshape.
        return errors
    errors.append((RequestValidationError, True))
    return errors


def _is_framework_default(app: "Starlette", klass: type[Exception]) -> bool:
    """Return whether the handler for `klass` is the one FastAPI installed.

    FastAPI registers its own handlers in the constructor, so "already
    registered" cannot mean "the app chose this". Only a handler the app
    put there is left alone, which is how it opts one error back out of the
    shared format.

    A plain Starlette app registers nothing, so a missing entry is the same
    answer as a default one.
    """
    registered = app.exception_handlers.get(klass)
    if registered is None:
        return True
    try:
        from fastapi.exception_handlers import (  # noqa: PLC0415
            http_exception_handler,
            request_validation_exception_handler,
        )
    except ImportError:  # pragma: no cover
        return False
    return registered in (
        http_exception_handler,
        request_validation_exception_handler,
    )


def _field_errors(exc: Exception) -> "list[dict[str, Any]]":
    """Return the field errors of a validation failure, without the input.

    `loc`, `msg` and `type` tell a client which part of the request to fix.
    `input` only repeats what the client just sent, and `ctx` carries an
    exception object that serializes to nothing useful. Both are dropped.
    """
    raw = getattr(exc, "errors", None)
    entries = raw() if callable(raw) else []
    return [
        {key: value for key, value in entry.items() if key in _FIELD_KEYS}
        for entry in entries
    ]


_FIELD_KEYS = frozenset({"loc", "msg", "type"})
"""Members of a field error that help a client fix its request."""


def _install_framework_handlers(
    app: "Starlette",
    http_error: "Callable[..., Any]",
    validation_error: "Callable[..., Any]",
) -> None:
    """Reshape the framework's own errors into the registered format.

    A handler the app registered itself is left alone. The one grelmicro
    installs for validation is recorded, so anything that documents the
    schema can tell whether grelmicro is still the one answering.
    """
    for klass, validates in _framework_errors():
        if not _is_framework_default(app, klass):
            continue
        handler = validation_error if validates else http_error
        app.add_exception_handler(klass, handler)
        if validates:
            app.state.grelmicro_validation_handler = handler


def _structured_detail(detail: Any) -> "dict[str, Any] | None":  # noqa: ANN401
    """Return the extension members a non-string `detail` becomes.

    FastAPI documents a dict or a list there, and dropping it would lose a
    payload the app meant the client to read.

    A mapping is what an extension member already is, so its entries are
    carried as members in their own right. Anything else goes under
    `errors`, the name RFC 9457 readers expect a list of sub-problems
    under. A name that would displace one of the five standard members is
    dropped by `build`, so merging is safe.
    """
    if detail is None or isinstance(detail, str):
        return None
    if isinstance(detail, dict):
        return dict(detail)
    return {"errors": detail}


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
