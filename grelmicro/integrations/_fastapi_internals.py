"""FastAPI's router as the route gates read it, its private parts included.

FastAPI dispatches a route of an included router through a context it
builds for each include: an `APIRoute` or a frontend group runs as itself
with the context in the scope, and any other route runs as a copy the
context holds. Frontend routes are matched after every other route. Every
private part of FastAPI the gates rely on is read here alone, and
`require(router)` fails install when a FastAPI release moved one. Without
FastAPI installed, nothing here is FastAPI's.
"""

from __future__ import annotations

from itertools import chain, repeat
from operator import eq, is_
from typing import TYPE_CHECKING, Any, Final

try:
    from fastapi import routing
except ImportError:  # pragma: no cover - the reimport test walks this
    routing = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

    Scope = MutableMapping[str, Any]

__all__ = [
    "ROUTERS",
    "candidates",
    "context_of",
    "copy_of",
    "gate_low_priority",
    "is_dispatched_as_itself",
    "is_included",
    "is_websocket_route",
    "low_priority_routes",
    "original_of",
    "path_of",
    "require",
    "router_of",
    "watch",
]

ROUTERS: Final[tuple[type[Any], ...]] = (
    () if routing is None else (routing.APIRouter,)
)
"""FastAPI's router class, when FastAPI is installed."""

_ROUTE: Final[Any] = getattr(routing, "APIRoute", None)
_WEBSOCKET_ROUTE: Final[Any] = getattr(routing, "APIWebSocketRoute", None)
_INCLUDED: Final[Any] = getattr(routing, "_IncludedRouter", None)
_CONTEXT: Final[Any] = getattr(routing, "_EffectiveRouteContext", None)
_FRONTEND: Final[Any] = getattr(routing, "_FrontendRouteGroup", None)
_SCOPE_KEY: Final[Any] = getattr(routing, "_FASTAPI_SCOPE_KEY", None)
_CONTEXT_KEY: Final[Any] = getattr(
    routing, "_FASTAPI_EFFECTIVE_ROUTE_CONTEXT_KEY", None
)
_CANDIDATES: Final = "effective_candidates"
_LOW_PRIORITY: Final = "effective_low_priority_routes"


def require(router: object) -> None:
    """Fail when FastAPI no longer has a part the route gates read of `router`.

    Raises:
        RuntimeError: Naming each missing part.
    """
    missing = [
        name
        for name, found in (
            ("_IncludedRouter", _INCLUDED),
            ("_EffectiveRouteContext", _CONTEXT),
            ("_FASTAPI_SCOPE_KEY", _SCOPE_KEY),
            ("_FASTAPI_EFFECTIVE_ROUTE_CONTEXT_KEY", _CONTEXT_KEY),
        )
        if found is None
    ]
    frontend = _FRONTEND is not None
    attributes = (
        (_INCLUDED, "_IncludedRouter", _CANDIDATES),
        (_INCLUDED if frontend else None, "_IncludedRouter", _LOW_PRIORITY),
        (router, "APIRouter", "_mark_routes_changed"),
        (router if frontend else None, "APIRouter", "_low_priority_routes"),
    )
    fields = (
        (_INCLUDED, "_IncludedRouter", "original_router"),
        (_CONTEXT, "_EffectiveRouteContext", "original_route"),
        (_CONTEXT, "_EffectiveRouteContext", "starlette_route"),
        (_CONTEXT, "_EffectiveRouteContext", "path"),
        (_CONTEXT, "_EffectiveRouteContext", "dependant"),  # codespell:ignore
        (
            _CONTEXT if frontend else None,
            "_EffectiveRouteContext",
            "frontend_prefix",
        ),
    )
    missing.extend(
        f"{name}.{part}"
        for kind, name, part in attributes
        if kind is not None and not hasattr(kind, part)
    )
    missing.extend(
        f"{name}.{part}"
        for kind, name, part in fields
        if kind is not None
        and part not in getattr(kind, "__dataclass_fields__", {})
    )
    if missing:
        msg = (
            f"FastAPI's router has no {', '.join(missing)}, which "
            f"micro.install(app) gates each route through. Install a FastAPI "
            f"release grelmicro supports."
        )
        raise RuntimeError(msg)


def is_dispatched_as_itself(route: object) -> bool:
    """Return whether an include dispatches `route` as itself, with its context in the scope.

    An `APIRoute`, and the group of an app's frontend routes.
    """
    return any(
        kind is not None and isinstance(route, kind)
        for kind in (_ROUTE, _FRONTEND)
    )


def is_websocket_route(route: object) -> bool:
    """Return whether `route` is a FastAPI websocket route, declaring through its dependencies."""
    return _WEBSOCKET_ROUTE is not None and isinstance(route, _WEBSOCKET_ROUTE)


def is_included(route: object) -> bool:
    """Return whether `route` is a router FastAPI included."""
    return _INCLUDED is not None and isinstance(route, _INCLUDED)


def low_priority_routes(router: Any) -> list[Any]:  # noqa: ANN401
    """Return the routes a router matches after every other, its frontend group.

    Empty on a FastAPI release without frontend routes.
    """
    return router._low_priority_routes if _FRONTEND is not None else []  # noqa: SLF001


def gate_low_priority(router: Any, routes: list[Any]) -> None:  # noqa: ANN401
    """Make `routes` the ones a router matches after every other."""
    router._low_priority_routes = routes  # noqa: SLF001


def candidates(included: Any, *, top: bool) -> list[Any]:  # noqa: ANN401
    """Return the contexts FastAPI dispatches through for an include.

    A `top` include, one a router holds itself, is matched through its
    frontend routes too.
    """
    found = list(getattr(included, _CANDIDATES)())
    if top and _FRONTEND is not None:
        found.extend(getattr(included, _LOW_PRIORITY)())
    return found


def router_of(included: Any) -> Any:  # noqa: ANN401
    """Return the router an include holds the routes of."""
    return included.original_router


def watch(
    included: Any,  # noqa: ANN401
    *,
    top: bool,
    changed: Callable[[list[Any]], None],
) -> None:
    """Call `changed` with each list of contexts FastAPI builds anew for an include.

    FastAPI builds them anew when told the routes of the included router
    changed, and dispatches through the list it returns, so `changed` sees
    each list before any request meets it. A `top` include, one a router
    holds itself, tells FastAPI first when a route under it was added,
    removed or edited in place without telling it.
    """
    names = (
        (_CANDIDATES, _LOW_PRIORITY)
        if top and _FRONTEND is not None
        else (_CANDIDATES,)
    )
    shape = _Shape(included.original_router) if top else None
    for name in names:
        build = getattr(included, name)
        setattr(included, name, _watched(build, build(), changed, shape))


def _watched(
    build: Callable[[], list[Any]],
    last: list[Any],
    changed: Callable[[list[Any]], None],
    shape: _Shape | None,
) -> Callable[[], list[Any]]:
    """Return `build`, calling `changed` with each list it returns other than `last`."""

    def watched() -> list[Any]:
        nonlocal last
        if shape is not None:
            shape.check()
        found = build()
        if found is not last:
            last = found
            changed(found)
        return found

    return watched


class _Shape:
    """What the routers under an include hold: each route, its regex and its methods."""

    __slots__ = ("items", "lists", "methods", "regexes", "router", "routers")

    def __init__(self, router: Any) -> None:  # noqa: ANN401
        """Hold what `router`, and each router its includes hold, hold now."""
        self.router = router
        self.routers: tuple[Any, ...] = ()
        self.lists: tuple[list[Any], ...] = ()
        self.items: tuple[Any, ...] = ()
        self.regexes: tuple[Any, ...] = ()
        self.methods: tuple[frozenset[str] | None, ...] = ()
        self._take()

    def _take(self) -> None:
        """Hold what the routers hold now."""
        routers: list[Any] = []
        pending = [self.router]
        while pending:
            router = pending.pop()
            if any(router is known for known in routers):
                continue
            routers.append(router)
            pending.extend(
                route.original_router
                for route in router.routes
                if isinstance(route, _INCLUDED)
            )
        self.routers = tuple(routers)
        self.lists = tuple(router.routes for router in routers)
        self.items = tuple(chain.from_iterable(self.lists))
        self.regexes = _regexes(self.items)
        self.methods = tuple(
            None if methods is None else frozenset(methods)
            for methods in _methods(self.items)
        )

    def check(self) -> None:
        """Tell FastAPI the routes changed, when any changed since last held."""
        lists = tuple(router.routes for router in self.routers)
        if (
            all(map(is_, lists, self.lists))
            and sum(map(len, lists)) == len(self.items)
            and all(map(is_, chain.from_iterable(lists), self.items))
            and all(map(is_, _regexes(self.items), self.regexes))
            and all(map(eq, _methods(self.items), self.methods))
        ):
            return
        for router in self.routers:
            router._mark_routes_changed()  # noqa: SLF001
        self._take()


def _regexes(routes: tuple[Any, ...]) -> tuple[Any, ...]:
    """Return the regex each route matches its path with, if any."""
    return tuple(map(getattr, routes, repeat("path_regex"), repeat(None)))


def _methods(routes: tuple[Any, ...]) -> tuple[Any, ...]:
    """Return the methods each route answers, if it names any."""
    return tuple(map(getattr, routes, repeat("methods"), repeat(None)))


def context_of(scope: Scope, route: object) -> Any | None:  # noqa: ANN401
    """Return the context FastAPI dispatches `route` through, if an include's."""
    fastapi = scope.get(_SCOPE_KEY)
    context = None if fastapi is None else fastapi.get(_CONTEXT_KEY)
    if context is None or context.original_route is not route:
        return None
    return context


def copy_of(context: Any) -> Any | None:  # noqa: ANN401
    """Return the copy of a route an include dispatches to, when it runs one."""
    return context.starlette_route


def original_of(context: Any) -> Any:  # noqa: ANN401
    """Return the route an include's context runs as itself."""
    return context.original_route


def path_of(context: Any) -> str:  # noqa: ANN401
    """Return the path of an include's context, under the include's prefix."""
    if _FRONTEND is not None and isinstance(context.original_route, _FRONTEND):
        return context.frontend_prefix
    return context.path
