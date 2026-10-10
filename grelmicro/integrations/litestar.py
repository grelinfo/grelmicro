"""Litestar integration that opens a Grelmicro app and binds it per request."""

from __future__ import annotations

import functools
import warnings
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from litestar import Litestar
from litestar._asgi.routing_trie.traversal import traverse_route_map
from litestar.exceptions import (
    HTTPException,
    LitestarException,
    MethodNotAllowedException,
)
from litestar.routes import BaseRoute
from litestar.status_codes import HTTP_405_METHOD_NOT_ALLOWED
from litestar.utils.path import normalize_path
from typing_extensions import Doc

from grelmicro._asgi import GrelmicroMiddleware
from grelmicro._component import authenticates, observes
from grelmicro._paths import (
    RouteReading,
    litestar_mount,
    litestar_owned_handler,
    read_route,
)
from grelmicro.errors import (
    MiddlewarePlacementWarning,
)
from grelmicro.health._served import HealthEndpoint, health_endpoint_in
from grelmicro.http import ErrorResponses, RateLimitMiddleware, merge_headers
from grelmicro.http._authentication import (
    ANONYMOUS_OPT,
    METADATA_MARKER,
    document_operations,
    metadata_path_of,
    operation_authentication,
    refuse_routes_at_metadata,
    resource_metadata_of,
    serves_anonymous_routes,
    template_under_root,
)
from grelmicro.http._kinds import BODYLESS_STATUSES, HANDLED, UNHANDLED_KEY
from grelmicro.http._openapi import add_error_schema
from grelmicro.http._requirement import (
    AUTHENTICATED,
    Requirement,
    declared_scopes,
)
from grelmicro.http._routes import RouteDeclaration
from grelmicro.trace._autoinstrument import request_spans

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Iterator,
        Mapping,
        MutableMapping,
        Sequence,
    )

    from litestar import Request
    from litestar.connection import ASGIConnection
    from litestar.handlers.base import BaseRouteHandler
    from litestar.response import Response

    from grelmicro import Grelmicro
    from grelmicro._paths import RouteReader
    from grelmicro.http import Gate
    from grelmicro.http._kinds import Unhandled
    from grelmicro.security.principal import VerifiedToken

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

    Raises:
        TypeError: If a flood limit would run behind the router, before
            anything is wired.
    """
    if any(
        declared_class is RateLimitMiddleware
        and arguments.get("flood") is not None
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
    would reach.
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
            # The public routes it serves without a credential. The rest of
            # ours name their paths in `include=` on Litestar.
            _route_resource_metadata(app, component)
            component.read_routes(app)
            component.document_openapi(app)


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
    passed = any(
        declared_class is RateLimitMiddleware
        for declared_class, _ in _routed_middleware(app)
    )
    if not (behind or passed):
        return
    for component in components:
        middleware, options = component.asgi_middleware()
        if (
            middleware is RateLimitMiddleware
            and options.get("flood") is not None
        ):
            raise TypeError(_FLOOD_BEHIND_ROUTER)


def _route_resource_metadata(app: Litestar, component: Any) -> None:  # noqa: ANN401
    """Add a route at the path the protected resource metadata is served at.

    A middleware passed to `Litestar(middleware=[...])` runs behind the
    router, which answers `404` to a path no route matches before that
    middleware sees it. The route serves the document itself, so it is
    found however the middleware was placed. A route the app declares at
    that path is refused, since it would never run. One reaching the path
    through a parameter routes the request on to the middleware already,
    so none is added then.

    Raises:
        TypeError: If the app declares a route at the metadata path.
    """
    from litestar import asgi  # noqa: PLC0415

    middleware, options = component.asgi_middleware()
    declared = [
        arguments
        for declared_class, arguments in _routed_middleware(app)
        if declared_class is middleware
    ]
    for described in declared or [options]:
        metadata = resource_metadata_of(described)
        if metadata is None:
            continue
        refuse_routes_at_metadata(app, metadata)
        if _routes(app, metadata.route):
            continue
        app.register(
            asgi(metadata.route, opt=Anonymous(), copy_scope=False)(
                metadata.document()
            )
        )


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
    registered, with the gate that component builds.

    Read more in the [Plugins](../architecture/plugins.md#declare-the-routes)
    docs.

    Raises:
        TypeError: If a declaration's `cache` is neither a boolean nor a
            `timedelta`.
        ValueError: If a declaration cannot hold, naming its route.
    """
    gated: dict[int, tuple[Any, ASGIApp]] = {}
    declared: dict[tuple[int, str], RouteDeclaration] = {}

    def gated_for(asgi_app: Any, handler: Any, template: str) -> ASGIApp:  # noqa: ANN401
        """Return the gated app of one route's handler, gating it the first time."""
        known = gated.get(id(asgi_app))
        if known is not None:
            return known[1]
        key = (id(handler), template)
        if key not in declared:
            declared.update(
                ((id(found), declaration.path), declaration)
                for found, declaration in _handler_declarations(app)
            )
        declaration = declared.get(key) or RouteDeclaration(template or "/")
        wrapped = gate(
            asgi_app,
            declaration,
            name=functools.partial(_gated_route, declaration.path),
        )
        gated[id(asgi_app)] = (asgi_app, wrapped)
        return wrapped

    _gate_router(app.asgi_router, gated_for)


def _gated_route(declared: str, scope: Scope) -> str:
    """Return the route a gate refused, as the app's route reader names it.

    Falls back to the path the route was declared with, under the root
    path, on an app no route reader was left for.
    """
    return read_route(scope) or template_under_root(declared, scope)


def _gate_router(
    router: Any,  # noqa: ANN401
    gated_for: Callable[[Any, Any, str], ASGIApp],
) -> None:
    """Make Litestar's router dispatch each handler through `gated_for`.

    Litestar's own router internals are touched here, in
    `_render_in_routes` and in `_trie_nodes` alone: the router gets a class
    of its own, whose `handle_routing` returns the gated app of the handler
    it matched, and whose `construct_routing_trie` gates each handler
    registered later.

    Raises:
        RuntimeError: If the router lacks what this relies on, naming it.
    """
    base = type(router)
    _require_router_internals(
        router, "handle_routing", "construct_routing_trie"
    )

    def handle_routing(self: Any, path: str, method: Any) -> Any:  # noqa: ANN401
        asgi_app, handler, routed, parameters, template = base.handle_routing(
            self, path, method
        )
        return (
            gated_for(asgi_app, handler, template),
            handler,
            routed,
            parameters,
            template,
        )

    def construct_routing_trie(self: Any) -> None:  # noqa: ANN401
        base.construct_routing_trie(self)
        for asgi_app, handler, template in _trie_handlers(self):
            gated_for(asgi_app, handler, template)

    router.__class__ = type(
        f"Gated{base.__name__}",
        (base,),
        {
            "__slots__": (),
            "handle_routing": functools.lru_cache(_ROUTING_CACHE)(
                handle_routing
            ),
            "construct_routing_trie": construct_routing_trie,
        },
    )
    for asgi_app, handler, template in _trie_handlers(router):
        gated_for(asgi_app, handler, template)


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


def _trie_handlers(router: Any) -> list[tuple[Any, Any, str]]:  # noqa: ANN401
    """Return each handler the router's trie dispatches to, its app and its template."""
    return [
        (entry.asgi_app, entry.handler, node.path_template)
        for node in _trie_nodes(router)
        for entry in node.asgi_handlers.values()
    ]


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


_ROUTING_CACHE: Final = 1024
"""How many routed paths and methods each gated router keeps, as Litestar's own does."""


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
    around it names. The `OPTIONS` handler Litestar adds needs a caller.
    """
    return [declaration for _, declaration in _handler_declarations(app)]


def _handler_declarations(app: Litestar) -> list[tuple[Any, RouteDeclaration]]:
    """Return every handler of the app beside what it declares."""
    found: list[tuple[Any, RouteDeclaration]] = []
    for route in app.routes:
        handlers = getattr(route, "route_handlers", None) or [
            route.route_handler  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        ]
        found.extend(
            (handler, _declaration(route.path_format, handler))
            for handler in handlers
        )
    return found


def _declaration(template: str, handler: Any) -> RouteDeclaration:  # noqa: ANN401
    """Return what one handler declares.

    The route grelmicro adds for the protected resource metadata is not
    anonymous.
    """
    methods = getattr(handler, "http_methods", None)
    return RouteDeclaration(
        template,
        methods=frozenset(methods) if methods else None,
        anonymous=bool(handler.opt.get(ANONYMOUS_OPT))
        and not getattr(handler.fn, METADATA_MARKER, False),
        scopes=frozenset(
            scope
            for guard in handler.resolve_guards()
            for scope in declared_scopes(guard) or ()
        ),
    )


def _routes(app: Litestar, path: str) -> bool:
    """Return whether Litestar's router answers `GET path` with a route."""
    try:
        app.asgi_router.handle_routing(path=path, method="GET")
    except HTTPException:
        return False
    return True


def Anonymous() -> dict[str, Any]:  # noqa: N802
    """Serve this route without a credential.

    Every route is authenticated once `AuthenticatedRequests` is registered.
    Pass this as the handler's `opt` on the one that is public:

    ```python
    from litestar import get

    from grelmicro.integrations.litestar import Anonymous


    @get("/catalog", opt=Anonymous())
    async def catalog() -> list[Product]: ...
    ```

    It is a mapping, so it merges with options of your own:
    `opt={**Anonymous(), "tag": "public"}`. `micro.install(app)` reads it off
    every handler, per method, so a public read keeps its writes
    authenticated, and the app is read again when it starts.

    A token sent to it is verified: a valid one is the caller in
    `request.user`, and one that does not verify is answered `401`.

    It is `{"exclude_from_auth": True}`, the key Litestar's own
    authentication middleware reads, so a handler written for that one is
    public here too.

    Read more in the [Authentication](../http/authentication.md) docs.
    """
    return {ANONYMOUS_OPT: True}


def Authenticated(  # noqa: N802
    *,
    scopes: Annotated[
        Sequence[str],
        Doc("Scopes the caller must hold, every one of them."),
    ] = (),
) -> Callable[[ASGIConnection, BaseRouteHandler], Awaitable[None]]:
    """Require an authenticated caller holding every scope named.

    A Litestar guard:

    ```python
    from litestar import delete

    from grelmicro.integrations.litestar import Authenticated


    @delete(
        "/orders/{order_id:int}",
        guards=[Authenticated(scopes=["orders:write"])],
    )
    async def cancel(order_id: int) -> None: ...
    ```

    A caller with no credential is answered `401`, and one lacking a scope
    `403`, whose `WWW-Authenticate` challenge names the scopes. It
    needs a registered `AuthenticatedRequests`, which verifies the token
    before the handler runs.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        TypeError: If `scopes` is a single string.
        ValueError: If a scope is not an OAuth scope token.
    """
    requirement = Requirement(scopes)

    async def authenticated(
        connection: ASGIConnection,
        handler: BaseRouteHandler,  # noqa: ARG001
    ) -> None:
        """Refuse a caller that is not authenticated or lacks a scope."""
        requirement.caller(cast("Scope", connection.scope))

    return requirement.declare(authenticated)


def current_token(
    connection: Annotated[
        ASGIConnection,
        Doc("The request or websocket the handler was handed."),
    ],
) -> VerifiedToken:
    """Return the bearer token the request presented, once it verified.

    ```python
    from litestar import Request, post

    from grelmicro.integrations.litestar import current_token


    @post("/orders")
    async def create_order(request: Request) -> dict[str, str]:
        token = current_token(request)
        response = await payments.post(
            "/charges", auth=payments_for_user.auth(token)
        )
        return response.json()
    ```

    `AuthenticatedRequests` leaves it on every request it authenticates.
    Read it to act for the caller, such as exchanging it with a
    `TokenExchange`. Its `repr` never shows the token.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        AuthenticationRequiredError: If the request carried no token that
            `AuthenticatedRequests` verified, such as one on a handler
            declaring `Anonymous()`.
    """
    return AUTHENTICATED.token(cast("Scope", connection.scope))


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
        and "public" in arguments
        and arguments["public"] is options.get("public")
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
            headers=merge_headers(rendered, getattr(exc, "headers", None)),
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
            headers=merge_headers(rendered, getattr(exc, "headers", None)),
        )

    def http_error(request: Request, exc: Exception) -> Response:
        """Reshape Litestar's own error into the registered format."""
        from litestar.exceptions import (  # noqa: PLC0415
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


def _document_authentication(app: Litestar, options: dict[str, Any]) -> None:
    """Describe the bearer token every covered operation needs, in the schema.

    Runs on startup, as the error responses do, so a handler registered
    after `install` is described too. A handler declaring `Anonymous()`
    lists it as optional when `micro.install(app)` added the middleware, and
    a path in `exclude` names none.
    """

    async def document() -> None:
        from litestar._openapi.plugin import (  # noqa: PLC0415
            OpenAPIPlugin,
        )

        if app.openapi_config is None:
            return
        registered = getattr(app.state, "grelmicro_error_responses", None)
        errors = ErrorResponses() if registered is None else registered
        public, scopes = operation_authentication(
            app, anonymous=serves_anonymous_routes(app)
        )
        document_operations(
            app.plugins.get(OpenAPIPlugin).provide_openapi_schema(),
            verifier=options["verifier"],
            bans=options["bans"] is not None,
            exclude=tuple(options["exclude"]),
            public=public,
            scopes=scopes,
            media_type=errors.media_type,
            model=errors.model,
            metadata_path=metadata_path_of(options),
        )

    app.on_startup.append(document)


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
        declared_class is GrelmicroMiddleware
        for declared_class, _ in _routed_middleware(app)
    )
