"""Litestar integration that opens a Grelmicro app and binds it per request."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from grelmicro._asgi import GrelmicroMiddleware
from grelmicro.errors import MiddlewarePlacementWarning
from grelmicro.http import ErrorResponses, merge_headers
from grelmicro.http._kinds import BODYLESS_STATUSES, HANDLED
from grelmicro.http._openapi import add_error_schema

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping, Sequence

    from litestar import Litestar, Request
    from litestar.response import Response

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
        Litestar,
        Doc("The Litestar application to wire."),
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
            "Wrap the app's ASGI handler with `GrelmicroMiddleware` so patterns "
            "resolve ambiently inside route handlers. Default `True`. Pass "
            "`False` to skip it."
        ),
    ] = True,
) -> None:
    """Wire `micro` into a Litestar app.

    Opens `async with micro:` on startup and closes it after shutdown, so the
    components are registered before the first request. Startup hooks and
    lifespan managers already passed to `Litestar(...)` keep running.

    When `ambient` is `True`, wraps the app's ASGI handler so patterns resolve
    through `Grelmicro.current()` inside route handlers. The wrap sits outside
    every middleware Litestar built, so one that resolves a backend ambiently
    always runs inside the request scope.

    Call it after the app is built, since Litestar builds its middleware stack
    at construction time:

    ```python
    from litestar import Litestar

    from grelmicro import Grelmicro

    micro = Grelmicro(uses=[...])
    app = Litestar(route_handlers=[...])
    micro.install(app)
    ```

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """

    async def _open_micro() -> None:
        await micro.__aenter__()

    async def _close_micro() -> None:
        if micro._exit_stack is not None:  # noqa: SLF001
            await micro.__aexit__(None, None, None)

    app.on_startup.append(_open_micro)
    app.on_shutdown.append(_close_micro)

    if not ambient:
        micro._on_ambient_disabled()  # noqa: SLF001
    elif not is_bound(app):
        # Litestar types its handler with its own ASGI aliases, which are
        # narrower than the mappings a pure-ASGI middleware accepts.
        handler = cast("ASGIApp", app.asgi_handler)
        app.asgi_handler = cast(
            "Any", GrelmicroMiddleware(handler, micro=micro)
        )


def install_middleware(
    app: Annotated[
        Litestar,
        Doc("The Litestar application to wire."),
    ],
    components: Annotated[
        Sequence[Any],
        Doc("The registered components that carry an ASGI middleware."),
    ],
) -> None:
    """Add the ASGI middleware each registered component asks for.

    A component that carries `asgi_middleware()` returns the middleware
    class and the arguments to build it with, and this wraps the app's ASGI
    handler with it. Registration order is wrapping order, so the first one
    registered is the outermost and answers first.

    Each one is wrapped inside the binding, so a middleware that resolves a
    backend ambiently finds the app bound.

    Call it after the app is built, since Litestar builds its middleware
    stack at construction time. `micro.install(app)` calls this with the
    components it found, so a direct call is only for an app that never goes
    through `install`.
    """
    for component in components:
        _answer_for(app, component)
    for component in reversed(components):
        middleware, options = component.asgi_middleware()
        if _already_wired(app, middleware):
            # The app passed it to `Litestar(middleware=...)`, which puts it
            # inside its own stack, which is the better place. Wrapping a
            # second one would run it twice.
            continue
        if not _observes(component):
            # A middleware that only watches belongs outside the app's
            # own, so wrapping the whole stack is where it should be.
            # Warning about it would send the reader to move it inside,
            # where a request an outer layer refuses never reaches it.
            _warn_if_wrapping_app_middleware(app, middleware)
        binding = app.asgi_handler
        if isinstance(binding, GrelmicroMiddleware):
            # Inside the binding, which `install` put outermost so a
            # middleware resolving a backend runs in the request scope.
            _wrap_inside(binding, middleware, options, app)
        else:
            app.asgi_handler = cast(
                "Any",
                _wrapped(
                    cast("ASGIApp", app.asgi_handler),
                    middleware,
                    options,
                    app,
                ),
            )


def _wrap_inside(
    binding: GrelmicroMiddleware,
    middleware: type[Any],
    options: dict[str, Any],
    app: Litestar,
) -> None:
    """Put the middleware under the binding, inside what handles errors."""
    binding.app = cast("Any", _wrapped(binding.app, middleware, options, app))


def _wrapped(
    handler: ASGIApp,
    middleware: type[Any],
    options: dict[str, Any],
    app: Litestar,
) -> ASGIApp:
    """Return `handler` wrapped, under whatever turns errors into responses.

    Litestar renders an unhandled exception into a response in the
    outermost layer of its own handler. Wrapping around that would hand a
    middleware of ours the framework's `500` as though the app had
    produced it, and an idempotent replay would serve that `500` for the
    whole window. Slipping underneath it means the exception reaches our
    middleware as an exception, exactly as it does on Starlette.

    Feature-detected rather than named: a layer that exposes the next ASGI
    app under `app` is one this can go under, and a release that stops
    doing so falls back to wrapping the whole thing.
    """
    host = _innermost_layer(handler, app)
    if host is None:  # pragma: no cover
        return middleware(handler, **options)
    host.app = middleware(cast("ASGIApp", host.app), **options)
    return handler


def _innermost_layer(handler: ASGIApp, app: Litestar) -> Any | None:  # noqa: ANN401
    """Return the layer that wraps the router, or None if there is none.

    Every layer the app built sits above the router: the one that renders
    an exception, and whatever else was configured, such as CORS. Taking
    one hop lands under the first of them and above the rest, which is
    only the same thing when there is exactly one.

    The walk stops at the router rather than going through it. The router
    holds the app itself, and reads the lifespan from that reference.
    """
    host = None
    seen = 0
    while seen < _MAX_CHAIN:
        inner = getattr(handler, "app", None)
        if inner is None or not callable(inner) or inner is app:
            return host
        host = handler
        handler = cast("ASGIApp", inner)
        seen += 1
    return host  # pragma: no cover


def _already_wired(app: Litestar, middleware: type[Any]) -> bool:
    """Return whether this middleware is already in front of the app.

    Either because the app passed it to `Litestar(middleware=[...])`, or
    because `install` ran before and wrapped it. One layer answers, stores
    and tags. Two would do all three twice.
    """
    declared = any(
        getattr(entry, "middleware", None) is middleware or entry is middleware
        for entry in getattr(app, "middleware", ())
    )
    return declared or _wrapped_already(app.asgi_handler, middleware)


def _wrapped_already(handler: object, middleware: type[Any]) -> bool:
    """Return whether the handler chain already holds one of these."""
    seen = 0
    while handler is not None and seen < _MAX_CHAIN:
        if isinstance(handler, middleware):
            return True
        handler = getattr(handler, "app", None)
        seen += 1
    return False


_MAX_CHAIN = 32
"""How far to walk a handler chain before calling it a cycle."""


def _observes(component: Any) -> bool:  # noqa: ANN401
    """Return whether this component's middleware only watches a request."""
    return bool(getattr(component, "asgi_observes", False))


def _warn_if_wrapping_app_middleware(
    app: Litestar, middleware: type[Any]
) -> None:
    """Warn when this wrap would sit outside the app's own middleware.

    Litestar builds its middleware stack when the app is constructed, so
    `install` can only wrap the whole thing. A middleware of ours that
    answers a request itself would then answer before the app's own
    middleware runs, authentication included. Passing it to
    `Litestar(middleware=[...])` puts it inside, which is where it belongs.
    """
    if not getattr(app, "middleware", ()):
        return
    warnings.warn(
        f"{middleware.__name__} wraps the Litestar app, so it runs outside "
        f"the middleware the app declares, authentication included. A "
        f"request it answers itself, such as an idempotent replay, would "
        f"not reach them. Pass DefineMiddleware({middleware.__name__}, "
        f"...) to Litestar(middleware=[...]) instead, which puts it inside "
        f"the stack. [middleware-placement] "
        f"https://grelmicro.grel.info/diagnostics/#middleware-placement",
        MiddlewarePlacementWarning,
        stacklevel=3,
    )


def _answer_for(app: Litestar, component: object) -> None:
    """Register a handler for what this component answers itself.

    A component that carries `handled_exceptions()` names the rejections
    its registration is the opt-in for, so a service that asked for them
    gets the status on the wire rather than a `500`. The format is read
    per request from whichever `ErrorResponses` the app registered, and is
    RFC 9457 when it registered none.

    A handler passed to `Litestar(exception_handlers=...)` wins, and so
    does the one `install_error_responses` registered, which renders the
    same way.
    """
    handled = getattr(component, "handled_exceptions", None)
    if handled is None:
        return

    def handler(request: Request, exc: Exception) -> Response:
        """Render one rejection in the format the app answers in."""
        from litestar.response import (  # noqa: PLC0415
            Response as LitestarResponse,
        )

        errors = (
            getattr(request.app.state, "grelmicro_error_responses", None)
            or ErrorResponses()
        )
        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return LitestarResponse(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    for klass in handled():
        if klass not in app.exception_handlers:
            app.exception_handlers[klass] = handler


def install_error_responses(
    app: Annotated[
        Litestar,
        Doc("The Litestar application to wire."),
    ],
    errors: Annotated[
        ErrorResponses,
        Doc("The registered component that renders each rejection."),
    ],
) -> None:
    """Render grelmicro rejections in a standard format on a Litestar app.

    Registers one exception handler per rejection grelmicro raises to turn a
    caller away, so a rate limiter, a bulkhead, an open circuit breaker, an
    elapsed deadline, or an idempotency conflict answers the client with an
    `application/problem+json` body instead of a `500`.

    Litestar looks a handler up through the raised exception's class
    hierarchy, so registering `AdmissionError` covers every rejection under
    it, including one a later release adds.

    Call it after the app is built and before it serves, since Litestar
    resolves each route's handlers on its first request. `micro.install(app)`
    calls this when `ErrorResponses()` is registered, so a direct call is only
    for an app that never goes through `install`.

    ```python
    from grelmicro.http import ErrorResponses
    from grelmicro.integrations.litestar import install_error_responses

    install_error_responses(app, ErrorResponses())
    ```

    Read more in the [Error Responses](../http/errors.md) docs.
    """

    def handler(request: Request, exc: Exception) -> Response:
        """Render one rejection, taking the occurrence from the request path."""
        from litestar.response import (  # noqa: PLC0415
            Response as LitestarResponse,
        )

        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return LitestarResponse(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    def http_error(request: Request, exc: Exception) -> Response:
        """Reshape Litestar's own error into the registered format."""
        from litestar.exceptions import (  # noqa: PLC0415
            HTTPException,
            ValidationException,
        )
        from litestar.response import (  # noqa: PLC0415
            Response as LitestarResponse,
        )

        http_exc = cast("HTTPException", exc)
        headers = http_exc.headers or {}
        if http_exc.status_code in BODYLESS_STATUSES:
            # A `204` or a `304` carries no body by the protocol, whatever
            # format the app answers in.
            return LitestarResponse(
                content=b"",
                status_code=http_exc.status_code,
                headers=headers,
            )
        if isinstance(exc, ValidationException):
            rendered = errors.render_validation(
                _field_errors(exc),
                status=http_exc.status_code,
                detail=http_exc.detail or None,
                instance=request.url.path,
            )
        else:
            rendered = errors.render_status(
                http_exc.status_code,
                detail=http_exc.detail or None,
                instance=request.url.path,
            )
        return LitestarResponse(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, headers),
        )

    app.state.grelmicro_error_responses = errors

    from litestar.exceptions import HTTPException  # noqa: PLC0415

    if HTTPException not in app.exception_handlers:
        app.exception_handlers[HTTPException] = http_error
        # Only now is what the framework describes no longer what the app
        # answers with. An app that kept its own handler keeps its own
        # schema too, or the two would disagree.
        _document_error_responses(app, errors)

    for klass in HANDLED:
        # A handler passed to `Litestar(exception_handlers=...)` wins, the
        # same way one registered before `install` does on Starlette.
        if klass not in app.exception_handlers:
            app.exception_handlers[klass] = handler


_LITESTAR_ERROR_MEMBERS = frozenset({"detail", "status_code"})
"""What every schema Litestar generates for its own exception requires.

Matched on the shape rather than on the description text, which is drawn
from a class docstring and is not a contract.
"""


def _document_error_responses(app: Litestar, errors: ErrorResponses) -> None:
    """Republish the schema's error responses in the format now installed.

    Litestar describes every error response with the shape of its own
    `HTTPException`. That is no longer what those operations answer with, so
    a generated client would decode the wrong body.

    Runs on startup rather than here. Litestar builds the schema lazily and
    caches it, and a route registered after `install` would be missing from
    one built too early.
    """

    async def rewrite() -> None:
        from litestar._openapi.plugin import (  # noqa: PLC0415
            OpenAPIPlugin,
        )

        if app.openapi_config is None:
            # `openapi_config=None` publishes no schema, and asking the
            # plugin to build one raises rather than returning nothing.
            return
        plugin = app.plugins.get(OpenAPIPlugin)
        # Built once and cached by the plugin, so rewriting the dict it
        # returns is what every later reader sees.
        _rewrite_error_responses(plugin.provide_openapi_schema(), errors)

    app.on_startup.append(rewrite)


def _rewrite_error_responses(
    schema: dict[str, Any], errors: ErrorResponses
) -> None:
    """Point every response Litestar generated at the registered body.

    The component is published through the shared helper, so a model the
    app already declared under that name is not replaced by ours.
    """
    targets = [
        response
        for path_item in schema.get("paths", {}).values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for response in operation.get("responses", {}).values()
        if _is_generated(response)
    ]
    if not targets:
        # Nothing answers with the model, so publishing it would leave the
        # schema carrying a component nothing points at.
        return
    ref = add_error_schema(schema, errors.model)
    if not ref:
        # Both names are taken by the app's own models. Litestar's own
        # entry is a better answer than one pointing at nothing.
        return
    for response in targets:
        response["content"] = {errors.media_type: {"schema": {"$ref": ref}}}


def _is_generated(response: dict[str, Any]) -> bool:
    """Return whether Litestar described this response with its own shape.

    A response the app declared itself, and one with no body at all, has no
    schema requiring Litestar's members and is left alone.
    """
    generated = (
        (response.get("content") or {})
        .get("application/json", {})
        .get("schema", {})
    )
    return set(generated.get("required", ())) >= _LITESTAR_ERROR_MEMBERS


def error_response(
    request: Annotated[
        Request,
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
) -> Response:
    """Answer from your own exception handler in the app's error format.

    The Litestar counterpart of the Starlette helper. The format is read
    from the app, so a service that registered `ErrorResponses.tmf()`
    answers in TMF from here too.

    ```python
    from grelmicro.integrations.litestar import error_response


    def handle(request: Request, exc: InsufficientFunds) -> Response:
        return error_response(
            request, status=409, detail="Not enough to cover this charge."
        )
    ```
    """
    from litestar.response import (  # noqa: PLC0415
        Response as LitestarResponse,
    )

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
    return LitestarResponse(
        content=rendered.body,
        status_code=rendered.status,
        media_type=rendered.media_type,
        headers=rendered.headers,
    )


def _field_errors(exc: Exception) -> list[dict[str, Any]]:
    """Return the field errors of a Litestar validation failure.

    Litestar puts them in `extra`, either as a list of entries or as a
    mapping. Both are normalised to the `loc`/`msg` shape every format
    renders, so a client reads the same thing whichever framework validated.
    """
    extra = getattr(exc, "extra", None)
    if isinstance(extra, list):
        return [
            {
                "loc": [entry.get("key")] if entry.get("key") else [],
                "msg": entry.get("message", ""),
            }
            if isinstance(entry, dict)
            else {"loc": [], "msg": str(entry)}
            for entry in extra
        ]
    if isinstance(extra, dict):
        return [
            {"loc": [key], "msg": str(value)} for key, value in extra.items()
        ]
    return []


def is_bound(
    app: Annotated[
        Litestar,
        Doc("The Litestar application to inspect."),
    ],
) -> bool:
    """Return whether the per-request binding middleware is in place.

    Called by `Grelmicro.check_ambient_binding` and `Grelmicro.describe` to
    catch an app that never had `micro.install(app)` called on it. True for
    both the wrap `install` adds and a `DefineMiddleware(GrelmicroMiddleware)`
    passed to `Litestar(middleware=[...])`.
    """
    if isinstance(getattr(app, "asgi_handler", None), GrelmicroMiddleware):
        return True
    return any(
        getattr(entry, "middleware", None) is GrelmicroMiddleware
        for entry in getattr(app, "middleware", ())
    )
