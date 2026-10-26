"""The route gates and route declarations of the Litestar integration.

`grelmicro.integrations.litestar` loads this module when it gates an app's
handlers or lists what they declare.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Final

from litestar import Litestar

from grelmicro._paths import read_route
from grelmicro.http._authentication import (
    ANONYMOUS_OPT,
    METADATA_MARKER,
    refuse_anonymous_caller,
    template_under_root,
)
from grelmicro.http._requirement import declared_scopes
from grelmicro.http._routes import RouteDeclaration
from grelmicro.integrations.litestar import (
    _require_router_internals,
    _trie_nodes,
)

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        MutableMapping,
        Sequence,
    )

    from litestar.routes import BaseRoute

    from grelmicro.http import Gate

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["declarations_of", "gate_handlers"]


def gate_handlers(app: Litestar, gate: Gate) -> None:
    """Gate every handler Litestar's router dispatches to, as `install_route_gate` describes.

    Raises:
        RuntimeError: If the router lacks what this relies on, naming it.
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
                ((id(found), path_format), declaration)
                for found, path_format, declaration in _handler_declarations(
                    app
                )
            )
        declaration = declared.get(key) or RouteDeclaration(template or "/")
        wrapped = gate(
            asgi_app,
            declaration,
            name=functools.partial(_gated_route, template or "/"),
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
    `_render_in_routes` and in `_trie_nodes` alone. The router gets a class
    of its own. Its `handle_routing` routes as the router did, then returns
    the gated app of the handler it matched. Its `construct_routing_trie`
    gates each handler registered later.

    Raises:
        RuntimeError: If the router lacks what this relies on, naming it.
    """
    base = type(router)
    _require_router_internals(
        router, "handle_routing", "construct_routing_trie"
    )

    routing = router.handle_routing

    def handle_routing(path: str, method: Any) -> Any:  # noqa: ANN401
        asgi_app, handler, routed, parameters, template = routing(path, method)
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
            "handle_routing": staticmethod(
                functools.lru_cache(_ROUTING_CACHE)(handle_routing)
            ),
            "construct_routing_trie": construct_routing_trie,
        },
    )
    for asgi_app, handler, template in _trie_handlers(router):
        gated_for(asgi_app, handler, template)


def _trie_handlers(router: Any) -> list[tuple[Any, Any, str]]:  # noqa: ANN401
    """Return each handler the router's trie dispatches to, its app and its template."""
    return [
        (entry.asgi_app, entry.handler, node.path_template)
        for node in _trie_nodes(router)
        for entry in node.asgi_handlers.values()
    ]


_ROUTING_CACHE: Final = 1024
"""How many routed paths and methods each gated router keeps, as Litestar's own does."""


def declarations_of(app: Litestar) -> list[RouteDeclaration]:
    """Return what every handler of the app requires, as `route_declarations` describes."""
    return [
        declaration
        for handler, _, declaration in _handler_declarations(app)
        if not getattr(handler.fn, METADATA_MARKER, False)
    ]


def _handler_declarations(
    app: Litestar,
) -> list[tuple[Any, str, RouteDeclaration]]:
    """Return every handler of the app beside its route's path and what it declares.

    The path is the route's own, as Litestar's router names it.
    """
    found: list[tuple[Any, str, RouteDeclaration]] = []
    for route in app.routes:
        handlers = getattr(route, "route_handlers", None) or [
            route.route_handler  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
        ]
        found.extend(
            (
                handler,
                route.path_format,
                _declaration(_template(route, handler), handler),
            )
            for handler in handlers
        )
    return found


def _template(route: BaseRoute, handler: Any) -> str:  # noqa: ANN401
    """Return the path a handler answers, a `path` parameter spanning segments.

    A mounted handler answers its path and every path under it.
    """
    template = route.path_format
    for name, parameter in route.path_parameters.items():
        if parameter.full.endswith(":path"):
            template = template.replace(f"{{{name}}}", f"{{{name}:path}}")
    if getattr(handler, "is_mount", False):
        template = f"{template.rstrip('/') or '/'}{{path:path}}"
    return template


def _declaration(template: str, handler: Any) -> RouteDeclaration:  # noqa: ANN401
    """Return what one handler declares.

    The route grelmicro adds for the protected resource metadata is not
    anonymous.

    Raises:
        TypeError: If the handler declares `Anonymous()` and an
            `Authenticated` guard, naming it.
    """
    methods = getattr(handler, "http_methods", None)
    guards = handler.resolve_guards()
    required = [declared_scopes(guard) for guard in guards]
    declaration = RouteDeclaration(
        template,
        methods=frozenset(methods) if methods else None,
        anonymous=bool(handler.opt.get(ANONYMOUS_OPT))
        and not getattr(handler.fn, METADATA_MARKER, False),
        scopes=frozenset(
            scope for scopes in required for scope in scopes or ()
        ),
        own_checks=_runs_own_checks(handler, guards),
    )
    if declaration.anonymous and any(scopes is not None for scopes in required):
        refuse_anonymous_caller(declaration)
    return declaration


def _runs_own_checks(handler: Any, guards: Sequence[Any]) -> bool:  # noqa: ANN401
    """Return whether a handler runs checks of its own before it.

    A guard other than `Authenticated` is one, and so are a dependency the
    handler asks for, a `before_request` hook, middleware on the handler,
    its controller or a router it sits under, and a mounted ASGI app.
    """
    if getattr(handler, "is_mount", False) or any(
        declared_scopes(guard) is None for guard in guards
    ):
        return True
    provided = handler.resolve_dependencies()
    if any(name in provided for name in handler.parsed_fn_signature.parameters):
        return True
    before = getattr(handler, "resolve_before_request", None)
    if before is not None and before() is not None:
        return True
    return any(
        layer.middleware
        for layer in handler.ownership_layers
        if not isinstance(layer, Litestar)
    )
