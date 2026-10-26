"""Litestar integration that opens a Grelmicro app and binds it per request."""

from __future__ import annotations

import functools
import sys
import warnings
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from litestar import Litestar
from litestar._asgi.routing_trie.traversal import traverse_route_map
from litestar.exceptions import (
    HTTPException,
    LitestarException,
    MethodNotAllowedException,
    NotFoundException,
)
from litestar.routes import BaseRoute
from litestar.status_codes import HTTP_405_METHOD_NOT_ALLOWED
from litestar.utils.path import normalize_path
from typing_extensions import Doc

from grelmicro._asgi import GrelmicroMiddleware
from grelmicro._component import authenticates, observes
from grelmicro._diagnostics import MIDDLEWARE_PLACEMENT, diagnostic
from grelmicro._environment import _PACKAGE_DIR
from grelmicro._paths import RouteReading
from grelmicro.errors import (
    MiddlewarePlacementWarning,
)
from grelmicro.health._served import HealthEndpoint, health_endpoint_in
from grelmicro.http._unhandled import UNHANDLED_KEY
from grelmicro.integrations._litestar_routes import (
    litestar_mount,
    litestar_owned_handler,
)

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Iterator,
        Mapping,
        MutableMapping,
        Sequence,
    )

    from grelmicro import Grelmicro
    from grelmicro._paths import RouteReader

    # Each `X as X` import re-exports a name served with a warning until 0.45.0.
    from grelmicro._paths import read_route as read_route
    from grelmicro.http import (
        AuthenticatedRequestsMiddleware as AuthenticatedRequestsMiddleware,
    )
    from grelmicro.http import ErrorResponses as ErrorResponses
    from grelmicro.http import Gate
    from grelmicro.http import RateLimitMiddleware as RateLimitMiddleware
    from grelmicro.http import RouteDeclaration as RouteDeclaration
    from grelmicro.http import merge_headers as merge_headers
    from grelmicro.http._authentication import ANONYMOUS_OPT as ANONYMOUS_OPT
    from grelmicro.http._authentication import (
        METADATA_MARKER as METADATA_MARKER,
    )
    from grelmicro.http._authentication import (
        describe_app_schema as describe_app_schema,
    )
    from grelmicro.http._authentication import (
        refuse_anonymous_caller as refuse_anonymous_caller,
    )
    from grelmicro.http._authentication import (
        refuse_routes_at_metadata as refuse_routes_at_metadata,
    )
    from grelmicro.http._authentication import (
        resource_metadata_of as resource_metadata_of,
    )
    from grelmicro.http._authentication import (
        template_under_root as template_under_root,
    )
    from grelmicro.http._kinds import BODYLESS_STATUSES as BODYLESS_STATUSES
    from grelmicro.http._kinds import HANDLED as HANDLED
    from grelmicro.http._openapi import add_error_schema as add_error_schema
    from grelmicro.http._openapi import describing as describing
    from grelmicro.http._requirement import AUTHENTICATED as AUTHENTICATED
    from grelmicro.http._requirement import Requirement as Requirement
    from grelmicro.http._requirement import declared_scopes as declared_scopes
    from grelmicro.http._unhandled import Unhandled
    from grelmicro.integrations._litestar_auth import (
        Anonymous,
        Authenticated,
        current_token,
    )
    from grelmicro.integrations._litestar_errors import error_response
    from grelmicro.integrations._litestar_openapi import (
        add_openapi_edit as add_openapi_edit,
    )
    from grelmicro.trace._autoinstrument import request_spans as request_spans

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "Anonymous",
    "Authenticated",
    "current_token",
    "error_response",
    "install",
    "install_error_responses",
    "install_middleware",
    "install_route_gate",
    "is_bound",
    "route_declarations",
]

_LAZY: dict[str, str] = {
    "Anonymous": "grelmicro.integrations._litestar_auth",
    "Authenticated": "grelmicro.integrations._litestar_auth",
    "current_token": "grelmicro.integrations._litestar_auth",
    "error_response": "grelmicro.integrations._litestar_errors",
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
            from grelmicro._deprecation import LITESTAR_MOVED  # noqa: PLC0415

            return LITESTAR_MOVED.load(name)
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})


def _loaded(module: str, name: str) -> Any | None:  # noqa: ANN401
    """Return `name` from `module`, or `None` while `module` is not loaded.

    An app can hold a class only once its module is loaded, so a class
    compared by identity is read from the loaded module alone.
    """
    loaded = sys.modules.get(module)
    return None if loaded is None else getattr(loaded, name, None)


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

    Adds an `after_exception` hook that marks a request whose handler raised
    an unhandled exception. The idempotency middleware stores nothing for a
    marked request.

    With `Trace` or `Metrics` registered, records the request span and the
    HTTP server metrics of every request. The request span, the access
    record and the security event of a request name the same route.

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

    Warns `MiddlewarePlacementWarning` when an
    `AuthenticatedRequestsMiddleware` the app passed to
    `Litestar(middleware=[...])` publishes metadata its router never routes.

    Raises:
        TypeError: If a flood limit would run behind the router, before
            anything is wired.
    """
    limiting = _loaded("grelmicro.http._ratelimit", "RateLimitMiddleware")
    if limiting is not None and any(
        declared_class is limiting and arguments.get("flood") is not None
        for declared_class, arguments in _routed_middleware(app)
    ):
        raise TypeError(_FLOOD_BEHIND_ROUTER)
    _refuse_flood(
        app,
        [
            component
            for component in micro.components
            if hasattr(component, "asgi_middleware")
        ],
    )
    _warn_if_metadata_unrouted(app, micro)

    async def _open_micro() -> None:
        await micro.__aenter__()

    async def _close_micro() -> None:
        if micro._exit_stack is not None:  # noqa: SLF001
            await micro.__aexit__(None, None, None)

    app.on_startup.append(_open_micro)
    app.on_shutdown.append(_close_micro)
    app.after_exception.append(cast("Any", _mark_unhandled))

    if not ambient:
        micro._on_ambient_disabled()  # noqa: SLF001
    elif not is_bound(app):
        # Litestar types its handler with its own ASGI aliases, which are
        # narrower than the mappings a pure-ASGI middleware accepts.
        handler = cast("ASGIApp", app.asgi_handler)
        app.asgi_handler = cast(
            "Any", GrelmicroMiddleware(handler, micro=micro)
        )
    reader = _wire_route_reading(app)
    _wire_request_telemetry(app, micro, reader)
    # Described once, by the error responses or the middleware installed
    # next, with every edit.
    _document_components(
        app,
        [
            component
            for component in micro.components
            if hasattr(component, "asgi_middleware")
        ],
        describe=False,
    )


def _wire_route_reading(app: Litestar) -> RouteReader:
    """Leave the route reader of `app` in the scope of every request, and return it.

    The reading wraps the app's ASGI handler, outside the binding. The
    first install wires it, and a later one returns the same reader.
    """
    reading = _wrapper_in(app.asgi_handler, RouteReading)
    if reading is not None:
        return reading.reader
    reader = _route_reader(app)
    options: dict[str, Any] = {"reader": reader}
    binding = app.asgi_handler
    if isinstance(binding, GrelmicroMiddleware):
        _wrap_outside(binding, RouteReading, options)
    else:
        app.asgi_handler = cast(
            "Any", RouteReading(cast("ASGIApp", binding), **options)
        )
    return reader


def _wire_request_telemetry(
    app: Litestar, micro: Grelmicro, reader: RouteReader
) -> None:
    """Record the request telemetry of `app` when `micro` exports it.

    The recorder goes over everything the app built, under the binding, and
    an `after_exception` hook hands it the exceptions no handler answers.
    `reader` names the route. A `Trace` that is off, or whose `instrument`
    leaves out `litestar`, turns the request spans off and keeps the
    metrics.
    """
    from grelmicro.trace._autoinstrument import request_spans  # noqa: PLC0415

    tracing = request_spans(micro.components, "litestar")
    if tracing is None:
        return
    from grelmicro.integrations._request_telemetry import (  # noqa: PLC0415
        RequestTelemetry,
        exceptions_on_spans,
        excluding,
        known_methods,
        normal_close,
        record_unhandled,
    )

    if _wrapped_already(app.asgi_handler, RequestTelemetry):
        return
    options: dict[str, Any] = {
        "route": reader,
        "tracing": tracing,
        "exclude": excluding("litestar"),
        "methods": known_methods(),
        "events": exceptions_on_spans(),
        "unwrap": _raised_by_app,
    }
    binding = app.asgi_handler
    if isinstance(binding, GrelmicroMiddleware):
        _wrap_outside(binding, RequestTelemetry, options)
    else:
        app.asgi_handler = cast(
            "Any", RequestTelemetry(cast("ASGIApp", binding), **options)
        )

    async def record(exc: Exception, scope: Scope) -> None:
        """Hand the request telemetry an exception no handler answers.

        An `HTTPException` is handled, and so is an exception a handler of
        the route catches on purpose. A WebSocket the client closed normally
        is how the connection ends, not a failure.
        """
        if isinstance(exc, HTTPException) or normal_close(exc):
            return
        route_handler = scope.get("route_handler")
        if route_handler is not None and _caught_on_purpose(
            exc, route_handler.resolve_exception_handlers()
        ):
            return
        record_unhandled(scope, exc)

    app.after_exception.append(cast("Any", record))


def _route_reader(app: Litestar) -> RouteReader:
    """Return the route reader of `app`.

    Router prefixes are included. A mounted ASGI app reads as `{path}`
    under its mount, and a mounted Litestar app as its own route under
    the mount. A request Litestar refused for its method reads the route
    its path matched. One answered before routing, such as a CORS
    preflight, reads no route, unless `reach` asks for the route its path
    would reach. There, a mount reads as `{path}` under it, whatever app
    its handler passes the request to.
    """

    def route(
        scope: Scope,
        root_path: str,
        path: str,
        status: int | None,
        /,
        *,
        reach: bool = False,
    ) -> str | None:
        handler = litestar_owned_handler(app, scope.get("route_handler"))
        template: str | None = None
        if scope.get("litestar_app") is app and handler is not None:
            template = _handler_template(app, handler, scope["path_template"])
        elif "route_handler" in scope:
            template = _mounted_template(app, scope, root_path, path)
        elif reach or status == HTTP_405_METHOD_NOT_ALLOWED:
            template = _reached_template(
                app, root_path, path, scope.get("method")
            )
        if template is None:
            return None
        return root_path.rstrip("/") + template

    return route


def _handler_template(app: Litestar, handler: Any, template: str) -> str:  # noqa: ANN401
    """Return the template of a handler `app` routed to, `{path}` for a mount."""
    if not getattr(handler, "is_mount", False):
        return template
    return litestar_mount(app, handler).rstrip("/") + "/{path}"


def _mounted_template(
    app: Litestar, scope: Scope, root_path: str, path: str
) -> str | None:
    """Return the template of the mount of `app` a mounted app took the request over at.

    A mounted Litestar app adds the route it matched under the mount.
    Any other reads as `{path}` under it.
    """
    template = _reached_template(app, root_path, path, scope.get("method"))
    inner = scope.get("litestar_app")
    inner_handler = litestar_owned_handler(inner, scope.get("route_handler"))
    inner_template = scope.get("path_template")
    if (
        template is not None
        and inner is not app
        and inner_handler is not None
        and isinstance(inner_template, str)
        and not getattr(inner_handler, "is_mount", False)
    ):
        return template.removesuffix("/{path}") + inner_template
    return template


def _reached_template(
    app: Litestar, root_path: str, path: str, method: str | None
) -> str | None:
    """Return the template of the route `app` routes the request's path to.

    Matched as Litestar's router matches it. A path refused for its
    method reads the route it matched, and a mount reads as `{path}`
    under it. `None` when no route of `app` matches the path.
    """
    routed = path.split(root_path, maxsplit=1)[-1] if root_path else path
    routed = normalize_path(routed)
    try:
        _, handler, _, _, template = app.asgi_router.handle_routing(
            routed, method
        )
    except MethodNotAllowedException:
        return _template_matching(app, routed)
    except Exception:  # noqa: BLE001
        return None
    return _handler_template(app, handler, template)


def _template_matching(app: Litestar, path: str) -> str:
    """Return the template of the route `path` matches, whatever the method.

    For a path the app matched and refused for its method, so a route
    holds it.
    """
    router = app.asgi_router
    if path in router._plain_routes:  # noqa: SLF001
        return router.root_route_map_node.children[path].path_template
    node, _, _ = traverse_route_map(
        root_node=router.root_route_map_node, path=path
    )
    return node.path_template


def _raised_by_app(exc: BaseException) -> BaseException:
    """Return the exception the app raised, from the one Litestar let out.

    Litestar wraps an exception raised once the response started in a bare
    `LitestarException`, once for each layer that renders exceptions, and a
    streamed body fails as a group of one.
    """
    while type(exc) is LitestarException and exc.__cause__ is not None:
        exc = exc.__cause__
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


async def _mark_unhandled(exc: Exception, scope: Scope) -> None:
    """Mark the request when the exception Litestar is about to render is unhandled.

    Runs as an `after_exception` hook, for a request that raised. Only a
    request idempotency may store is marked, and only once it was routed.
    An `HTTPException` is handled. Any other exception is unhandled unless
    `_caught_on_purpose` says a handler of the route answers it.
    """
    unhandled: Unhandled | None = scope.get(UNHANDLED_KEY)
    route_handler = scope.get("route_handler")
    if (
        unhandled is None
        or route_handler is None
        or isinstance(exc, HTTPException)
    ):
        return
    if not _caught_on_purpose(exc, route_handler.resolve_exception_handlers()):
        unhandled.raised = True


_CATCH_ALL: Final = (Exception, BaseException)
"""Classes whose exception handler answers any crash."""


def _caught_on_purpose(exc: Exception, handlers: Mapping[Any, Any]) -> bool:
    """Return whether a handler answers `exc` for its own class or a base of it.

    The first class of its MRO holding a handler decides. A handler for
    `Exception` or `BaseException` is a catch-all and does not count, and
    neither does one registered for status `500`.
    """
    for klass in type(exc).__mro__:
        if klass in handlers:
            return klass not in _CATCH_ALL
    return False


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

    One that authenticates, `AuthenticatedRequests`, is wrapped outermost
    among ours whatever order it was registered in, so none of ours serves
    a request that was never authenticated.

    Each one is wrapped inside the binding, so a middleware that resolves a
    backend ambiently finds the app bound.

    Once one that answers is wrapped, every route renders its own
    exceptions, as Litestar does for a route that runs middleware. A raised
    `HTTPException` then reaches each of ours as the response the caller
    receives, so it is stored, replayed, and carries the fields ours add.

    Call it after the app is built, since Litestar builds its middleware
    stack at construction time. `micro.install(app)` calls this with the
    components it found, so a direct call is only for an app that never goes
    through `install`.

    Raises:
        TypeError: If a flood limit would run behind the router.
    """
    # Authentication first, whatever order it was registered in. Stable, so
    # registration order holds among the rest.
    _refuse_flood(app, components)
    ordered = sorted(
        components, key=lambda component: not authenticates(component)
    )
    for component in ordered:
        _answer_for(app, component)
    # One that answers goes in underneath the ones already placed, so those
    # are wrapped in order and the first answers first. One that only
    # watches goes on top of everything, so those are wrapped in reverse for
    # the first of them to end up outermost.
    wrapping = [
        *(component for component in ordered if not observes(component)),
        *reversed([component for component in ordered if observes(component)]),
    ]
    behind = any(
        authenticates(component) and _declared_by_app(app, component)
        for component in ordered
    )
    for component in wrapping:
        middleware, options = component.asgi_middleware()
        watching = observes(component)
        if _already_wired(app, middleware) or (behind and not watching):
            # The app passed it to `Litestar(middleware=...)`, which puts it
            # inside its own stack, which is the better place. Wrapping a
            # second one would run it twice. Authentication the app runs
            # behind its router runs the others once it admitted a request.
            continue
        if not watching:
            _warn_if_wrapping_app_middleware(app, middleware)
            _render_in_routes(app)
        binding = app.asgi_handler
        if isinstance(binding, GrelmicroMiddleware):
            # Inside the binding, which `install` put outermost so a
            # middleware resolving a backend runs in the request scope.
            if watching:
                _wrap_outside(binding, middleware, options)
            else:
                _wrap_inside(binding, middleware, options, app)
        elif watching:
            app.asgi_handler = middleware(app.asgi_handler, **options)
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
    for component in ordered:
        if authenticates(component):
            from grelmicro.integrations._litestar_auth import (  # noqa: PLC0415
                route_resource_metadata,
            )

            # The public routes it serves without a credential. The rest of
            # ours name their paths in `include=` on Litestar.
            route_resource_metadata(app, component)
            component.read_routes(app)
    _document_components(app, components)


_FLOOD_BEHIND_ROUTER = (
    "A flood= limit runs before routing, and a middleware passed to "
    "Litestar(middleware=[...]) runs behind the router, where a URL no "
    "handler answers never reaches it. Register RateLimitedRequests and "
    "AuthenticatedRequests with Grelmicro(uses=[...]) instead, or drop flood=."
)
"""Why a flood limit behind Litestar's router is refused."""


def _refuse_flood(app: Litestar, components: Sequence[Any]) -> None:
    """Refuse a flood limit that would end up behind the router, or nowhere.

    Behind the router when the app passed authentication to
    `Litestar(middleware=[...])`. Nowhere when it passed its own
    `RateLimitMiddleware` there, which stands in for the component's.

    Raises:
        TypeError: If a component carrying a flood limit is in either case.
    """
    behind = any(
        authenticates(component) and _declared_by_app(app, component)
        for component in components
    )
    limiting = _loaded("grelmicro.http._ratelimit", "RateLimitMiddleware")
    passed = limiting is not None and any(
        declared_class is limiting
        for declared_class, _ in _routed_middleware(app)
    )
    if not (behind or passed):
        return
    for component in components:
        middleware, options = component.asgi_middleware()
        if options.get("flood") is not None and middleware is _loaded(
            "grelmicro.http._ratelimit", "RateLimitMiddleware"
        ):
            raise TypeError(_FLOOD_BEHIND_ROUTER)


def install_route_gate(
    app: Annotated[
        Litestar,
        Doc("The Litestar application whose handlers to gate."),
    ],
    gate: Annotated[
        Gate,
        Doc(
            "Returns the app to dispatch to in place of a handler, given it "
            "and the handler's declaration, refusing a declaration that "
            "cannot hold."
        ),
    ],
) -> None:
    """Gate every handler Litestar's router dispatches to, once it matched it.

    Each handler gets the gate its declaration asks for, per method: the
    `OPTIONS` handler Litestar adds to a route, a websocket handler and an
    ASGI mount included. A handler registered later is gated as it lands.

    `micro.install(app)` calls this when `AuthenticatedRequests` is
    registered, with the gate that component builds, and otherwise when
    `CachedResponses`, `IdempotentRequests` or `ConditionalRequests` is,
    with a gate that refuses nothing and runs them at the route.

    Read more in the [Plugins](../architecture/plugins.md#declare-the-routes)
    docs.

    Raises:
        TypeError: If a declaration's `cache` is neither a boolean nor a
            `timedelta`.
        ValueError: If a declaration cannot hold, naming its route.
    """
    from grelmicro.integrations._litestar_gate import (  # noqa: PLC0415
        gate_handlers,
    )

    gate_handlers(app, gate)


def _require_router_internals(router: Any, *methods: str) -> None:  # noqa: ANN401
    """Raise when Litestar's router lacks an internal grelmicro relies on.

    `methods` names the methods of its class the caller overrides. Its
    routing trie is always required.

    Raises:
        RuntimeError: Naming each one missing.
    """
    base = type(router)
    missing = [
        name
        for owner, name in (
            *((base, method) for method in methods),
            (router, "root_route_map_node"),
            (router, "_mount_routes"),
        )
        if not hasattr(owner, name)
    ]
    if missing:
        msg = (
            f"Litestar's router has no {', '.join(missing)}, which "
            f"micro.install(app) wires each route through. Install a "
            f"Litestar release grelmicro supports."
        )
        raise RuntimeError(msg)


def _trie_nodes(router: Any) -> list[Any]:  # noqa: ANN401
    """Return every node of the router's trie, mounts included.

    Read off Litestar's routing trie, one of the router internals
    `_gate_router` and `_render_in_routes` rely on.
    """
    found: list[Any] = []
    seen: set[int] = set()
    pending = [
        router.root_route_map_node,
        *router._mount_routes.values(),  # noqa: SLF001
    ]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        found.append(node)
        pending.extend(node.children.values())
    return found


def _render_in_routes(app: Litestar) -> None:
    """Make every route the app dispatches to render its own exceptions.

    Litestar renders an exception inside a route only when the route runs
    middleware. Otherwise it renders it above the whole handler, and a
    middleware of ours sees the exception instead of the response the
    caller receives. A route Litestar built no middleware for gets the
    renderer it gives one that runs some. So does one registered later.
    Rendering in the route runs the app's `after_exception` hooks as
    before, and the one that marks an unhandled exception is added when
    `install` did not add it, so idempotency stores no rendered crash.

    Raises:
        RuntimeError: If the router lacks what this relies on, naming it.
    """
    if _mark_unhandled not in app.after_exception:
        app.after_exception.append(cast("Any", _mark_unhandled))
    router = app.asgi_router
    base = type(router)
    if getattr(base, "_grelmicro_renders_in_routes", False):
        return
    _require_router_internals(router, "construct_routing_trie")

    def construct_routing_trie(self: Any) -> None:  # noqa: ANN401
        base.construct_routing_trie(self)
        _render_in(self)

    router.__class__ = type(
        f"Rendering{base.__name__}",
        (base,),
        {
            "__slots__": (),
            "_grelmicro_renders_in_routes": True,
            "construct_routing_trie": construct_routing_trie,
        },
    )
    _render_in(router)


def _render_in(router: Any) -> None:  # noqa: ANN401
    """Wrap each route the router dispatches straight to in Litestar's renderer.

    Litestar dispatches straight to a route's own `handle` when it built no
    middleware for it, and wraps every route it built middleware for in its
    renderer first.
    """
    from litestar._asgi.utils import wrap_in_exception_handler  # noqa: PLC0415

    for node in _trie_nodes(router):
        for key, entry in list(node.asgi_handlers.items()):
            if isinstance(getattr(entry.asgi_app, "__self__", None), BaseRoute):
                node.asgi_handlers[key] = entry._replace(
                    asgi_app=wrap_in_exception_handler(entry.asgi_app)
                )


def health_endpoints(
    app: Annotated[
        Litestar,
        Doc("The Litestar application whose health endpoints to list."),
    ],
) -> Iterator[HealthEndpoint]:
    """Yield the health endpoints the app serves, an ASGI mount included."""
    for route in app.routes:
        handlers = getattr(route, "route_handlers", None) or [
            route.route_handler  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        ]
        for handler in handlers:
            found = health_endpoint_in(getattr(handler, "fn", None))
            if found is not None:
                yield found


def route_declarations(
    app: Annotated[
        Litestar,
        Doc("The Litestar application whose handlers to list."),
    ],
) -> list[RouteDeclaration]:
    """Return what every handler of the app requires, as its gate reads it.

    One declaration per handler. A handler declaring `Anonymous()` is
    anonymous, and its scopes are the ones every `Authenticated` guard
    around it names. Any other guard, a dependency the handler asks for, a
    `before_request` hook, middleware on the handler, its controller or a
    router, and a mounted ASGI app run checks of their own. The `OPTIONS`
    handler Litestar adds needs a caller. The handler grelmicro registers
    for the protected resource metadata is not listed.
    """
    from grelmicro.integrations._litestar_gate import (  # noqa: PLC0415
        declarations_of,
    )

    return declarations_of(app)


def _wrap_outside(
    binding: GrelmicroMiddleware,
    middleware: type[Any],
    options: dict[str, Any],
) -> None:
    """Put the middleware over everything the app built, under the binding.

    A middleware that only watches wants the request as the caller sent it
    and the answer as the caller receives it. Litestar renders a
    `404`, a `405` and every `HTTPException` in the outermost layer of its
    own handler, so a watcher underneath that sees an exception where the
    caller saw a status, and records a `500` for a request that answered
    `404`.

    It answers nothing itself, so being above the app's own middleware
    cannot make a request skip any of it.
    """
    binding.app = middleware(binding.app, **options)


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


def _front_middleware(app: Litestar) -> list[tuple[Any, dict[str, Any]]]:
    """Return the middleware the app runs in front of its router.

    Each middleware wrapping `app.asgi_handler`, outermost first, as its
    class and the `policies` it carries, the ones `install_middleware`
    wrapped included. `micro.install(app)` reads it to find the
    authentication in front of the app.
    """
    found: list[tuple[Any, dict[str, Any]]] = []
    node: object = app.asgi_handler
    while node is not None and len(found) < _MAX_CHAIN:
        found.append(
            (type(node), {"policies": getattr(node, "_policies", None)})
        )
        node = getattr(node, "app", None)
    return found


def _routed_middleware(app: Litestar) -> list[tuple[Any, dict[str, Any]]]:
    """Return the middleware the app runs once its router matched a handler.

    Each is what the app passed to `Litestar(middleware=[...])`, as its
    class and the arguments it is built with. `micro.install(app)` reads
    it to find an `AuthenticatedRequests` middleware the app runs behind
    its router.
    """
    return [
        (
            getattr(entry, "middleware", entry),
            dict(getattr(entry, "kwargs", {})),
        )
        for entry in getattr(app, "middleware", ())
    ]


def _already_wired(app: Litestar, middleware: type[Any]) -> bool:
    """Return whether this middleware is already in front of the app.

    Either because the app passed it to `Litestar(middleware=[...])`, or
    because `install` ran before and wrapped it. One layer answers, stores
    and tags. Two would do all three twice.
    """
    passed = any(
        declared_class is middleware
        for declared_class, _ in _routed_middleware(app)
    )
    return passed or _wrapped_already(app.asgi_handler, middleware)


def _declared_by_app(app: Litestar, component: Any) -> bool:  # noqa: ANN401
    """Return whether the app runs this component's own middleware itself.

    That is, the app passed it to `Litestar(middleware=[...])`, built with
    the arguments the component builds it with.
    """
    middleware, options = component.asgi_middleware()
    return any(
        declared_class is middleware
        and "policies" in arguments
        and arguments["policies"] is options.get("policies")
        for declared_class, arguments in _routed_middleware(app)
    )


def _wrapped_already(handler: object, middleware: type[Any]) -> bool:
    """Return whether the handler chain already holds one of these."""
    return _wrapper_in(handler, middleware) is not None


def _wrapper_in[M](handler: object, middleware: type[M]) -> M | None:
    """Return the first of these the handler chain holds, if any."""
    seen = 0
    while handler is not None and seen < _MAX_CHAIN:
        if isinstance(handler, middleware):
            return handler
        handler = getattr(handler, "app", None)
        seen += 1
    return None


_MAX_CHAIN = 32
"""How far to walk a handler chain before calling it a cycle."""


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
        diagnostic(
            MIDDLEWARE_PLACEMENT,
            f"{middleware.__name__} wraps the Litestar app, so it runs "
            f"outside the middleware the app declares, authentication "
            f"included. A request it answers itself, such as an idempotent "
            f"replay, would not reach them. Pass DefineMiddleware("
            f"{middleware.__name__}, ...) to Litestar(middleware=[...]) "
            f"instead, which puts it inside the stack.",
        ),
        MiddlewarePlacementWarning,
        stacklevel=3,
    )


def _warn_if_metadata_unrouted(app: Litestar, micro: Grelmicro) -> None:
    """Warn when a `GET` for the metadata would never reach the middleware.

    For each `AuthenticatedRequestsMiddleware` the app passed to
    `Litestar(middleware=[...])` with `resource=`, which Litestar runs behind
    its router. Without a route at the metadata path, a `GET` for the
    document every challenge points at is answered `404`. With a route
    there for other methods only, it is answered `405`. Nothing is warned
    when `AuthenticatedRequests` is registered, which adds that route.
    """
    if any(authenticates(component) for component in micro.components):
        return
    authenticating = _loaded(
        "grelmicro.http._authentication", "AuthenticatedRequestsMiddleware"
    )
    for middleware, options in _routed_middleware(app):
        if not (
            authenticating is not None
            and isinstance(middleware, type)
            and issubclass(middleware, authenticating)
        ):
            continue
        from grelmicro.http._authentication import (  # noqa: PLC0415
            resource_metadata_of,
        )

        metadata = resource_metadata_of(options)
        if metadata is not None:
            _warn_unrouted(app, metadata.route)


def _warn_unrouted(app: Litestar, path: str) -> None:
    """Warn when Litestar's router does not route a `GET` to `path`."""
    try:
        app.asgi_router.handle_routing(path=path, method="GET")
    except NotFoundException:
        reason = (
            f"has no route at {path}, so the protected resource metadata "
            f"every challenge points at is answered 404. Register "
            f"AuthenticatedRequests and call micro.install(app), which adds "
            f"that route."
        )
    except HTTPException:
        reason = (
            f"routes {path} only for other methods, so a GET for the "
            f"protected resource metadata every challenge points at is "
            f"refused. Remove that route: the middleware answers every "
            f"request at that path."
        )
    else:
        return
    warnings.warn(
        diagnostic(
            MIDDLEWARE_PLACEMENT,
            f"AuthenticatedRequestsMiddleware runs behind Litestar's router, "
            f"which {reason}",
        ),
        MiddlewarePlacementWarning,
        skip_file_prefixes=(_PACKAGE_DIR,),
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
    from grelmicro.integrations._litestar_errors import (  # noqa: PLC0415
        answer_for,
    )

    answer_for(app, handled())


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
    from grelmicro.integrations._litestar_errors import (  # noqa: PLC0415
        wire_error_responses,
    )

    wire_error_responses(app, errors)


def _document_components(
    app: Litestar,
    components: Sequence[Any],
    *,
    describe: bool = True,
) -> None:
    """Hand the schema Litestar builds to each component that describes itself.

    Applied as the error responses are. Each component carrying
    `_document_openapi` is handed the schema in the order it was
    registered, whatever order its middleware runs in, so one describes
    the responses the ones registered before it added. It is handed the
    route declarations and the registered error format too. A handler
    declaring `Anonymous()` lists the scheme as optional when
    `micro.install(app)` added authentication.

    Added once per set of components, so the call `install` makes and the
    one `install_middleware` makes hand the schema to them once, and a
    second `Grelmicro` installed on the app adds its own. With `describe`
    false the current schema is left for a later edit to describe, with
    this one.
    """
    documenting = _describing(components)
    if not documenting:
        return
    from grelmicro.http._authentication import (  # noqa: PLC0415
        describe_app_schema,
    )
    from grelmicro.integrations._litestar_openapi import (  # noqa: PLC0415
        add_openapi_edit,
    )

    add_openapi_edit(
        app,
        functools.partial(describe_app_schema, app, components=documenting),
        key=("components", *map(id, documenting)),
        describe=describe,
    )


def _describing(components: Sequence[Any]) -> list[Any]:
    """Return the components that describe themselves, in order.

    Loads the schema helpers only when a component carries
    `_document_openapi`.
    """
    if not any(
        hasattr(component, "_document_openapi") for component in components
    ):
        return []
    from grelmicro.http._openapi import describing  # noqa: PLC0415

    return describing(components)


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
        declared_class is GrelmicroMiddleware
        for declared_class, _ in _routed_middleware(app)
    )
