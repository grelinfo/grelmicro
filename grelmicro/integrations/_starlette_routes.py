"""How a Starlette router names the route a request takes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

from typing_extensions import Doc

from grelmicro.integrations import _fastapi_internals as fastapi

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping, Sequence

__all__ = ["starlette_route"]


def starlette_route(  # noqa: C901
    router: Annotated[Any, Doc("The Starlette router the request entered.")],  # noqa: ANN401
    scope: Annotated[
        MutableMapping[str, Any], Doc("The scope, after routing.")
    ],
    root_path: Annotated[str, Doc("The root path the request arrived with.")],
    path: Annotated[str, Doc("The path the request arrived with.")],
    status: Annotated[int | None, Doc("The status it was answered with.")],
    *,
    mounted: Annotated[
        Callable[[MutableMapping[str, Any]], str | None],
        Doc("Reads the route a mounted app of another framework matched."),
    ],
    reaching: Annotated[
        Callable[[Any, MutableMapping[str, Any]], str | None] | None,
        Doc(
            "Reads the route a mounted app of another framework would "
            "route a request to, given the app and the scope under its mount."
        ),
    ] = None,
    reach: Annotated[
        bool,
        Doc("Name the route a request no router reached would reach."),
    ] = False,
) -> str | None:
    """Return the route template a Starlette router matched, under the root path.

    The template is each mount the request went through, then the route
    the innermost router matched. A request that went through no mount
    reads that route alone. One that did is matched again for the
    template of each mount, whether or not its router recorded the mount,
    and through any middleware wrapping a mounted app. When no router
    inside the last mount recorded a route, the one matching again finds
    is read, then the one `mounted` reads, else `{path}`. A FastAPI
    route reached through included routers reads with every prefix. A
    request a router redirects to add or drop a trailing slash reads the
    route it is redirected to.

    A request no router reached names no route. With `reach`, its path is
    matched against the routes of `router` as the router would match it.
    Under a mount of an app of another framework, the route `reaching`
    reads follows the mount. A mount nothing inside matches reads as
    `{path}` under it.
    """
    from starlette.routing import BaseRoute, Host, Match, Mount  # noqa: PLC0415
    from starlette.status import HTTP_307_TEMPORARY_REDIRECT  # noqa: PLC0415

    def template(scope: MutableMapping[str, Any], route: object) -> str | None:
        context = fastapi.context_of(scope, route)
        found = getattr(context or route, "path_format", None)
        return found if isinstance(found, str) else None

    def through_mounts(
        routes: Sequence[Any],
        scope: MutableMapping[str, Any],
        foreign: Callable[[Any, MutableMapping[str, Any]], str | None]
        | None = None,
    ) -> tuple[str | None, str | None]:
        """Return the templates of the mounts `scope` goes through, and of its route.

        Matches as Starlette's router does: the first full match, else the
        first partial one. The mounts are `None` when the request goes
        through none. The route is `None` when nothing inside the last
        mount matches. When that mount holds an app of another framework,
        it is the one `foreign` reads, `None` without it.
        """
        found = None
        for candidate in routes:
            match, child = candidate.matches(scope)
            if match is Match.FULL:
                found = candidate, child
                break
            if match is Match.PARTIAL and found is None:
                found = candidate, child
        if found is None:
            return None, None
        candidate, child = found
        if fastapi.is_included(candidate):
            candidate, child = fastapi.dispatched(candidate, {**scope, **child})
        if not isinstance(candidate, (Mount, Host)):
            return None, template(scope, candidate)
        prefix = (
            candidate.path_format.removesuffix("/{path}")
            if isinstance(candidate, Mount)
            else ""
        )
        inner = _inner_routes(candidate)
        if not inner or not all(
            isinstance(entry, BaseRoute) for entry in inner
        ):
            if foreign is None:
                return prefix, None
            return prefix, foreign(candidate.app, {**scope, **child})
        mounts, leaf = through_mounts(inner, {**scope, **child}, foreign)
        return prefix + (mounts or ""), leaf

    def start(at: str) -> dict[str, Any]:
        return {**scope, "path": at, "root_path": root_path, "path_params": {}}

    def redirected() -> str | None:
        """Return the route a slash redirect sends the request to, if any."""
        if scope["type"] != "http" or status != HTTP_307_TEMPORARY_REDIRECT:
            return None
        toggled = path.removesuffix("/") if path.endswith("/") else path + "/"
        mounts, leaf = through_mounts(router.routes, start(toggled))
        return None if leaf is None else (mounts or "") + leaf

    def reached() -> str | None:
        """Return the route the request's path would reach, if any."""
        mounts, leaf = through_mounts(router.routes, start(path), reaching)
        if leaf is not None:
            return (mounts or "") + leaf
        return None if mounts is None else mounts + "/{path}"

    if scope.get("router") is None:
        return reached() if reach else None
    matched = scope.get("route")
    route = matched is not None and not isinstance(matched, (Mount, Host))
    if route and scope.get("root_path", "") == root_path:
        return template(scope, matched)
    mounts, walked = through_mounts(router.routes, start(path))
    if mounts is None:
        return redirected() if matched is None else template(scope, matched)
    if route:
        walked = template(scope, matched)
    elif walked is None:
        walked = mounted(scope)
    if walked is not None:
        return mounts + walked
    return redirected() or mounts + "/{path}"


def _routing_app(app: Any) -> Any:  # noqa: ANN401
    """Unwrap ASGI middleware until a routing node or an application exposing routes is reached."""
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        bound = _bound_router(app)
        if bound is not None:
            app = bound
            continue
        if _is_route(app):
            return app
        nested = _wrapped_app(app)
        if nested is not None:
            app = nested
            continue
        return app
    return app


def _bound_router(app: Any) -> Any | None:  # noqa: ANN401
    """Return the router owning a bound `Router.app` method."""
    owner = getattr(app, "__self__", None)
    if (
        owner is None
        or getattr(app, "__name__", None) != "app"
        or not hasattr(owner, "routes")
    ):
        return None
    return owner


def _is_route(app: Any) -> bool:  # noqa: ANN401
    """Return whether `app` is a Starlette routing node, such as a `Mount` or a `Host`."""
    return _is_route_type(type(app))


_ROUTE_TYPES: Final[dict[type, bool]] = {}
"""Whether each class seen is a Starlette routing node class."""


def _is_route_type(kind: type) -> bool:
    """Return whether `kind` is a Starlette routing node class."""
    found = _ROUTE_TYPES.get(kind)
    if found is None:
        found = _ROUTE_TYPES[kind] = any(
            klass.__module__ == "starlette.routing"
            and klass.__name__ == "BaseRoute"
            for klass in kind.__mro__
        )
    return found


def _inner_routes(node: Any) -> Any:  # noqa: ANN401
    """Return the routes a request goes on to under a Starlette `Mount` or `Host`.

    A mounted routing node, such as another `Mount`, a `Host` or a
    `Route`, is the one route, so it matches with its own path or host.
    Otherwise they are the routes of the router it mounts.
    """
    app = getattr(node, "app", None)
    routes = getattr(node, "routes", None)
    if routes and not _is_route_type(type(app)):
        return routes
    unwrapped = _routing_app(app)
    if _is_route(unwrapped):
        return [unwrapped]
    return routes or getattr(unwrapped, "routes", None)


def _wrapped_app(app: Any) -> Any | None:  # noqa: ANN401
    """Return the application an explicit ASGI wrapper delegates to."""
    nested = getattr(app, "app", None)
    if (
        nested is None
        or nested is app
        or getattr(nested, "__self__", None) is app
    ):
        return None
    return nested
