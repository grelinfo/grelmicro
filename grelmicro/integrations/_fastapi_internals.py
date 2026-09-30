"""FastAPI's router as the route gates read it, its private parts included.

FastAPI dispatches a route of an included router through a context it
builds for each include: an `APIRoute` or a frontend group runs as itself
with the context in the scope, and any other route runs as a copy the
context holds. Frontend routes are matched after every other route. Every
private part of FastAPI the gates rely on is read here alone, and
`require()` fails install when a FastAPI release moved one. Without
FastAPI installed, nothing here is FastAPI's.
"""

from __future__ import annotations

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
    "watch",
]

ROUTERS: Final[tuple[type[Any], ...]] = (
    () if routing is None else (routing.APIRouter,)
)
"""FastAPI's router class, when FastAPI is installed."""

_ROUTE: Final[Any] = getattr(routing, "APIRoute", None)
_WEBSOCKET_ROUTE: Final[Any] = getattr(routing, "APIWebSocketRoute", None)
_INCLUDED: Final[Any] = getattr(routing, "_IncludedRouter", None)
_FRONTEND: Final[Any] = getattr(routing, "_FrontendRouteGroup", None)
_SCOPE_KEY: Final[Any] = getattr(routing, "_FASTAPI_SCOPE_KEY", None)
_CONTEXT_KEY: Final[Any] = getattr(
    routing, "_FASTAPI_EFFECTIVE_ROUTE_CONTEXT_KEY", None
)
_CANDIDATES: Final = "effective_candidates"
_LOW_PRIORITY: Final = "effective_low_priority_routes"


def require() -> None:
    """Fail when FastAPI no longer has what the route gates rely on.

    Raises:
        RuntimeError: Naming each missing part.
    """
    missing = [
        name
        for name, found in (
            ("_IncludedRouter", _INCLUDED),
            ("_FASTAPI_SCOPE_KEY", _SCOPE_KEY),
            ("_FASTAPI_EFFECTIVE_ROUTE_CONTEXT_KEY", _CONTEXT_KEY),
        )
        if found is None
    ]
    if _INCLUDED is not None and not hasattr(_INCLUDED, _CANDIDATES):
        missing.append(f"_IncludedRouter.{_CANDIDATES}")
    if _FRONTEND is not None and not hasattr(_INCLUDED, _LOW_PRIORITY):
        missing.append(f"_IncludedRouter.{_LOW_PRIORITY}")
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
    """Return the routes a router matches after every other, its frontend group."""
    return getattr(router, "_low_priority_routes", [])


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


def watch(
    included: Any,  # noqa: ANN401
    *,
    top: bool,
    changed: Callable[[list[Any]], None],
) -> None:
    """Call `changed` with each list of contexts FastAPI builds anew for an include.

    FastAPI builds them anew when the routes of the included router
    changed, and dispatches through the list it returns, so `changed` sees
    each list before any request meets it.
    """
    names = (
        (_CANDIDATES, _LOW_PRIORITY)
        if top and _FRONTEND is not None
        else (_CANDIDATES,)
    )
    for name in names:
        build = getattr(included, name)
        setattr(included, name, _watched(build, build(), changed))


def _watched(
    build: Callable[[], list[Any]],
    last: list[Any],
    changed: Callable[[list[Any]], None],
) -> Callable[[], list[Any]]:
    """Return `build`, calling `changed` with each list it returns other than `last`."""

    def watched() -> list[Any]:
        nonlocal last
        found = build()
        if found is not last:
            last = found
            changed(found)
        return found

    return watched


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
    return context.path or context.frontend_prefix
