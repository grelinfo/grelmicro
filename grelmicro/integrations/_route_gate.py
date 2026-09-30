"""Gate every route a Starlette router dispatches to, and list what each declares.

One walk serves both: `route_declarations` collects what it finds, and
`install_route_gate` wraps each route's `handle` with the gate its
declarations ask for, ahead of the route's own `405`.

A route added later is gated as it lands in a router's route list. On each
request a router checks that its route list and its default are the ones
it gated, and a mount or a host that its app is, and gates what replaced
them before dispatching.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any, Final, Protocol, Self, SupportsIndex

from starlette.applications import Starlette
from starlette.endpoints import HTTPEndpoint
from starlette.routing import (
    BaseRoute,
    Host,
    Mount,
    Route,
    Router,
    WebSocketRoute,
)

from grelmicro.http._authentication import (
    is_anonymous_declaration,
    template_under_root,
)
from grelmicro.http._requirement import declared_scopes, declares_optional
from grelmicro.http._response_cache import declared_cache
from grelmicro.http._routes import RouteDeclaration
from grelmicro.integrations import _fastapi_internals as fastapi

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, MutableMapping

    from grelmicro.http import Gate

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["declarations_of", "gate_routes"]

_ENDPOINT_METHODS: Final = (
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
)
"""The methods an `HTTPEndpoint` answers from a method of its own."""

_PREFIX_KEY: Final = "grelmicro.route_prefix"
"""Where each mount a request comes through adds its path, as its route declares it."""

_HELD: Final = "_grelmicro_held"
"""Where a router, a mount or a host keeps what it was gated with."""

_TARGET: Final = "_grelmicro_gated"
"""Where a FastAPI route, or an include's context of it, keeps its gated handler."""

_READS: Final = frozenset({"GET", "HEAD"})
"""The methods whose responses may be cached."""

_ROUTERS: Final = (Router, *fastapi.ROUTERS)
"""The routers whose routes are read, of their framework's own class."""


class _Visitor(Protocol):
    """What a walk hands each thing it finds."""

    def router(self, router: Router, prefix: str) -> None:
        """Take a router, found under `prefix`, before its routes."""

    def mount(self, mount: Mount | Host, prefix: str) -> _Visitor:
        """Take a mount or a host, before what is under it.

        Returns what takes the routes under it.
        """

    def door(
        self,
        mount: Mount | Host,
        whole: RouteDeclaration | None,
        *,
        opens: bool,
    ) -> None:
        """Take the declaration a mount or a host is gated as a whole with, if any.

        Once what is under it was walked. `opens` says a route under it
        serves a caller with no credential.
        """

    def app(self, app: Starlette, prefix: str) -> None:
        """Take a Starlette app a mount serves, found under `prefix`."""

    def route(
        self,
        owner: Any,  # noqa: ANN401
        attribute: str,
        path: str,
        declarations: list[RouteDeclaration],
    ) -> None:
        """Take what `owner.attribute` dispatches to, and what it declares."""

    def included(
        self,
        included: Any,  # noqa: ANN401
        prefix: str,
        *,
        top: bool,
    ) -> None:
        """Take a router FastAPI included, found under `prefix`, before its routes."""

    def contextual(
        self,
        route: Any,  # noqa: ANN401
        context: Any,  # noqa: ANN401
        declaration: RouteDeclaration,
    ) -> None:
        """Take a FastAPI route dispatched as itself, through `context` or directly."""


def _walk_router(
    router: Router, prefix: str, visit: _Visitor, ancestry: frozenset[int]
) -> bool:
    """Walk every route of a router, and its default when it has one of its own.

    Returns whether a route under it serves a caller with no credential.
    """
    if id(router) in ancestry:
        return False
    ancestry |= {id(router)}
    last: list[Any] = []
    if isinstance(router, fastapi.ROUTERS):
        fastapi.require(router)
        last = list(fastapi.low_priority_routes(router))
    visit.router(router, prefix)
    opens = False
    for route in list(router.routes):
        opens |= _walk_route(route, prefix, visit, ancestry, router)
    _walk_default(router, prefix, visit)
    for route in last:
        opens |= _walk_route(route, prefix, visit, ancestry, router)
    return opens


def _walk_route(
    route: Any,  # noqa: ANN401
    prefix: str,
    visit: _Visitor,
    ancestry: frozenset[int],
    router: Any,  # noqa: ANN401
) -> bool:
    """Walk one route of `router`, and the router behind a mount or a host.

    A mount or a host whose app is not a Starlette router is gated as one
    protected route, and the router found behind the app, if any, is
    walked too. Returns whether a route it leads to serves a caller with
    no credential.
    """
    if isinstance(route, Mount | Host):
        under = f"{prefix}{route.path}" if isinstance(route, Mount) else prefix
        inner, app = _inner_router(route.app)
        within = visit.mount(route, prefix)
        if app is not None:
            within.app(app, under)
        opens = inner is not None and _walk_router(
            inner, under, within, ancestry
        )
        whole = None if inner is route.app else RouteDeclaration(under or "/")
        visit.door(route, whole, opens=opens)
        return opens
    path = f"{prefix}{getattr(route, 'path', '')}" or "/"
    if fastapi.is_included(route):
        return _walk_included(route, prefix, visit, ancestry, top=True)
    if fastapi.is_dispatched_as_itself(route):
        declaration = _dependant_declaration(route, route, path, router)
        visit.contextual(route, None, declaration)
        return declaration.anonymous
    declarations = (
        [_dependant_declaration(route, route, path, router)]
        if fastapi.is_websocket_route(route)
        else _declarations(route, path)
    )
    visit.route(route, "handle", path, declarations)
    return any(declaration.anonymous for declaration in declarations)


def _walk_included(
    included: Any,  # noqa: ANN401
    prefix: str,
    visit: _Visitor,
    ancestry: frozenset[int],
    *,
    top: bool,
) -> bool:
    """Walk what FastAPI dispatches to through an included router.

    A `top` include, one a router holds itself, is walked with its
    frontend routes.
    """
    visit.included(included, prefix, top=top)
    router = fastapi.router_of(included)
    opens = False
    for candidate in fastapi.candidates(included, top=top):
        opens |= _walk_candidate(candidate, prefix, visit, ancestry, router)
    return opens


def _walk_candidate(
    candidate: Any,  # noqa: ANN401
    prefix: str,
    visit: _Visitor,
    ancestry: frozenset[int],
    router: Any,  # noqa: ANN401
) -> bool:
    """Walk one context FastAPI built for an include of `router`, or an include within it."""
    if fastapi.is_included(candidate):
        return _walk_included(candidate, prefix, visit, ancestry, top=False)
    copy = fastapi.copy_of(candidate)
    if copy is not None:
        return _walk_route(copy, prefix, visit, ancestry, router)
    path = f"{prefix}{fastapi.path_of(candidate)}" or "/"
    route = fastapi.original_of(candidate)
    declaration = _dependant_declaration(candidate, route, path, router)
    visit.contextual(route, candidate, declaration)
    return declaration.anonymous


def _walk_default(router: Router, prefix: str, visit: _Visitor) -> None:
    """Walk a router's default, unless it is the router's own `404`."""
    default = router.default
    if (
        getattr(default, "__func__", None) is Router.not_found
        and getattr(default, "__self__", None) is router
    ):
        return
    visit.route(router, "default", prefix or "/", [])


def _inner_router(
    app: Any,  # noqa: ANN401
) -> tuple[Router | None, Starlette | None]:
    """Return the Starlette router an app routes with, and the Starlette app holding it.

    What wraps the app is looked through. Only a router of Starlette's own
    class is read, and an app's router when the app is a Starlette app.
    Anything else is not read.
    """
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        if type(app) in _ROUTERS:
            return app, None
        if isinstance(app, Starlette):
            router = app.router
            return (router if type(router) in _ROUTERS else None), app
        if isinstance(app, BaseRoute | Router):
            return None, None
        app = getattr(app, "app", None)
    return None, None


def _declarations(route: Any, path: str) -> list[RouteDeclaration]:  # noqa: ANN401
    """Return what a route declares, one declaration per method set.

    A function endpoint declares the scopes its `@Authenticated` names, for
    the methods its route answers. An `HTTPEndpoint` declares each of its
    methods on its own. Any other route is authenticated.
    """
    if not isinstance(route, Route | WebSocketRoute):
        return [RouteDeclaration(path)]
    endpoint = route.endpoint
    if isinstance(route, WebSocketRoute):
        return [RouteDeclaration(path, scopes=_scopes(endpoint))]
    methods = frozenset(route.methods) if route.methods else None
    if isinstance(endpoint, type) and issubclass(endpoint, HTTPEndpoint):
        return _endpoint_declarations(endpoint, path, methods)
    return [RouteDeclaration(path, methods=methods, scopes=_scopes(endpoint))]


def _endpoint_declarations(
    endpoint: type[HTTPEndpoint],
    path: str,
    methods: frozenset[str] | None,
) -> list[RouteDeclaration]:
    """Return what each method of an `HTTPEndpoint` declares, grouped by scopes.

    `HEAD` is answered by `get` when the class has no `head`, as the
    endpoint dispatches it. A method the class does not answer is left to
    the route's own gate.
    """
    grouped: dict[frozenset[str], set[str]] = {}
    for method in methods or _ENDPOINT_METHODS:
        name = method.lower()
        if method == "HEAD" and getattr(endpoint, "head", None) is None:
            name = "get"
        handler = getattr(endpoint, name, None)
        if handler is None:
            continue
        grouped.setdefault(_scopes(handler), set()).add(method)
    return [
        RouteDeclaration(path, methods=frozenset(answered), scopes=scopes)
        for scopes, answered in sorted(
            grouped.items(), key=lambda item: sorted(item[1])
        )
    ]


def _scopes(target: object) -> frozenset[str]:
    """Return the scopes an `@Authenticated` on `target` requires."""
    return frozenset(declared_scopes(target) or ())


def _dependant_declaration(
    owner: Any,  # noqa: ANN401
    route: Any,  # noqa: ANN401
    path: str,
    router: Any,  # noqa: ANN401
) -> RouteDeclaration:
    """Return what a FastAPI route declares through the dependencies it runs.

    `owner` is the route, or the context an include dispatches it through,
    and `router` the router holding the route. It is anonymous when one of
    its own dependencies is `Anonymous()`, and requires the scopes of every
    `Security` around each dependency that requires the caller. Any other
    dependency is a check of its own, except `CachedResponse()` and
    `OptionalPrincipal`. A `CachedResponse()` written on the route is
    declared as it is, and one a router declares caches a read that runs
    no check of its own.
    """
    declared = owner.dependant.dependencies  # codespell:ignore
    above = {
        id(depends.dependency)
        for depends in getattr(router, "dependencies", ())
    }
    written = {
        id(dependency.call)
        for dependency in route.dependant.dependencies  # codespell:ignore
    } - above
    anonymous = False
    cache: bool | float = False
    kept_here: bool | float = False
    for dependency in declared:
        call = dependency.call
        anonymous = anonymous or is_anonymous_declaration(call)
        kept = declared_cache(call)
        if kept is False:
            continue
        if id(call) in written:
            kept_here = kept
        else:
            cache = kept
    scopes: set[str] = set()
    own_checks = False
    pending = list(declared)
    while pending:
        dependency = pending.pop()
        call = dependency.call
        if declared_scopes(call) is not None:
            scopes.update(dependency.parent_oauth_scopes or ())
            scopes.update(dependency.own_oauth_scopes or ())
        elif not (
            is_anonymous_declaration(call)
            or declares_optional(call)
            or declared_cache(call) is not False
        ):
            own_checks = True
            pending.extend(dependency.dependencies)
    methods = frozenset(getattr(owner, "methods", None) or ()) or None
    if kept_here is not False:
        cache = kept_here
    elif own_checks or methods is None or not methods <= _READS:
        cache = False
    return RouteDeclaration(
        path,
        methods=methods,
        anonymous=anonymous,
        scopes=frozenset(scopes),
        own_checks=own_checks,
        cache=cache,
    )


class _Listing:
    """Collects every declaration a walk finds."""

    def __init__(self) -> None:
        """Start with none."""
        self.found: list[RouteDeclaration] = []
        self._mounts: list[int] = []

    def router(self, router: Router, prefix: str) -> None:
        """List nothing for a router, whose routes are listed on their own."""

    def mount(
        self,
        mount: Mount | Host,  # noqa: ARG002
        prefix: str,  # noqa: ARG002
    ) -> _Listing:
        """Keep the place of a mount, ahead of what is under it."""
        self._mounts.append(len(self.found))
        return self

    def door(
        self,
        mount: Mount | Host,  # noqa: ARG002
        whole: RouteDeclaration | None,
        *,
        opens: bool,
    ) -> None:
        """List what a mount is gated as a whole with, unless a route under it opens it."""
        place = self._mounts.pop()
        if whole is not None and not opens:
            self.found.insert(place, whole)

    def app(self, app: Starlette, prefix: str) -> None:
        """List nothing for an app, whose router is listed on its own."""

    def route(
        self,
        owner: Any,  # noqa: ANN401, ARG002
        attribute: str,  # noqa: ARG002
        path: str,
        declarations: list[RouteDeclaration],
    ) -> None:
        """List what one route declares, or the default a router answers with."""
        self.found.extend(declarations or [RouteDeclaration(path)])

    def included(
        self,
        included: Any,  # noqa: ANN401
        prefix: str,
        *,
        top: bool,
    ) -> None:
        """List nothing for an include, whose routes are listed on their own."""

    def contextual(
        self,
        route: Any,  # noqa: ANN401, ARG002
        context: Any,  # noqa: ANN401, ARG002
        declaration: RouteDeclaration,
    ) -> None:
        """List what a FastAPI route declares where it is dispatched."""
        self.found.append(declaration)


def declarations_of(app: Starlette) -> list[RouteDeclaration]:
    """Return what every route of `app` declares, walked as the gates are."""
    listing = _Listing()
    _walk_router(app.router, "", listing, frozenset())
    return listing.found


class _Held:
    """What a router, a mount or a host was gated with, and what it held then.

    `within` is each mount it sits under, and a mount's `beneath` what it
    brings up to date before its open door lets a request through.
    """

    __slots__ = (
        "app",
        "beneath",
        "default",
        "door",
        "gates",
        "handle",
        "prefixes",
        "router",
        "routes",
        "shut",
        "size",
        "within",
    )

    def __init__(self, handle: Any) -> None:  # noqa: ANN401
        """Hold nothing yet, keeping the `handle` it dispatched with."""
        self.gates: list[Gate] = []
        self.prefixes: list[str] = []
        self.within: list[_Held] = []
        self.beneath: dict[int, Callable[[], object]] = {}
        self.handle = handle
        self.door: Any = None
        self.shut: Any = None
        self.routes: Any = None
        self.size = 0
        self.default: Any = None
        self.app: Any = None
        self.router: Any = None

    def settle(self, *, opens: bool) -> None:
        """Let a mount's requests through ungated while `opens`, and refuse them otherwise."""
        self.door = self.handle if opens or self.shut is None else self.shut


class _Gating:
    """Wraps what each route dispatches to with its gates, and holds each router."""

    def __init__(self, gates: list[Gate], within: Iterable[_Held] = ()) -> None:
        """Gate with every gate in `gates`, the first wrapping and the others counting.

        `within` is each mount the routes sit under.
        """
        self.gates = gates
        self.within = tuple(within)

    def _hold(self, owner: Any, prefix: str) -> tuple[_Held, bool]:  # noqa: ANN401
        """Return what `owner` is held with, and whether it was held just now."""
        held = owner.__dict__.get(_HELD)
        new = held is None
        if held is None:
            held = owner.__dict__[_HELD] = _Held(getattr(owner, "handle", None))
        for gate in self.gates:
            if all(gate is not known for known in held.gates):
                held.gates.append(gate)
        if prefix not in held.prefixes:
            held.prefixes.append(prefix)
        held.within.extend(
            mount for mount in self.within if mount not in held.within
        )
        return held, new

    def _beneath(self, owner: Any, refresh: Callable[[], object]) -> None:  # noqa: ANN401
        """Have each mount the routes sit under call `refresh` before its open door."""
        for mount in self.within:
            mount.beneath[id(owner)] = refresh

    def settle(self) -> None:
        """Open the door of each mount the routes sit under while a route under it is anonymous."""
        for mount in self.within:
            inner = _inner_router(mount.app)[0]
            mount.settle(
                opens=inner is not None
                and _walk_router(inner, "", _Listing(), frozenset())
            )

    def _gated(
        self,
        app: ASGIApp,
        declarations: list[RouteDeclaration],
        name: Callable[[Scope], str],
        *,
        door: bool = False,
    ) -> ASGIApp:
        """Return `app` gated by every gate, its refusals named by `name`.

        A door runs no lane.
        """
        for gate in self.gates:
            app = gate(app, *declarations, name=name, door=door)
        return app

    def router(self, router: Router, prefix: str) -> None:
        """Hold `router`, checking its route list and its default on each request."""
        held, new = self._hold(router, prefix)
        held.routes = _own_routes(router)
        held.size = len(held.routes)
        held.default = router.default
        if isinstance(router, fastapi.ROUTERS):
            low = fastapi.low_priority_routes(router)
            if not isinstance(low, _GatedRoutes):
                fastapi.gate_low_priority(router, _GatedRoutes(low, router))
        if new:
            router.middleware_stack = _guarded_router(  # type: ignore[assignment]
                router, held, router.middleware_stack
            )
        self._beneath(router, functools.partial(_refresh, router, held))

    def mount(self, mount: Mount | Host, prefix: str) -> _Gating:
        """Hold `mount`, and return what gates the routes under it."""
        held, _ = self._hold(mount, prefix)
        return _Gating(self.gates, (*self.within, held))

    def door(
        self,
        mount: Mount | Host,
        whole: RouteDeclaration | None,
        *,
        opens: bool,
    ) -> None:
        """Gate `mount` as a whole with `whole`, if any, checking its app on each request.

        Its door lets a request through ungated while `opens`.
        """
        held: _Held = mount.__dict__[_HELD]
        if held.app is not mount.app:
            held.app = mount.app
            held.shut = None
            mount.handle = _guarded_mount(mount, held)  # type: ignore[method-assign,assignment]  # ty: ignore[invalid-assignment]
        if whole is not None:
            held.shut = self._gated(
                held.shut or held.handle,
                [whole],
                _UNDER_MOUNTS,
                door=_inner_router(mount.app)[0] is not None,
            )
        held.settle(opens=opens)

    def app(self, app: Starlette, prefix: str) -> None:
        """Hold a mounted Starlette app, gating the router it builds its stack with.

        A stack the app built before it was held is dropped, so its next
        request builds one with the router this walk gated.
        """
        held, new = self._hold(app, prefix)
        held.router = app.router
        if not new:
            return
        app.middleware_stack = None
        build = app.build_middleware_stack

        def build_middleware_stack() -> ASGIApp:
            if app.router is not held.router:
                gating = _Gating(held.gates, held.within)
                for under in tuple(held.prefixes):
                    _walk_router(app.router, under, gating, frozenset())
                gating.settle()
                held.router = app.router
            return build()

        app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    def route(
        self,
        owner: Any,  # noqa: ANN401
        attribute: str,
        path: str,
        declarations: list[RouteDeclaration],
    ) -> None:
        """Wrap `owner.attribute` with the gate its declarations ask for.

        Each declaration is handed to every gate at every path the route
        sits under, and the route is wrapped once. A refusal names the
        route by its own path under the mounts the request came through.
        """
        own = getattr(owner, "path", "") if attribute == "handle" else ""
        setattr(
            owner,
            attribute,
            self._gated(
                getattr(owner, attribute),
                declarations or [RouteDeclaration(path)],
                functools.partial(_path_under_mounts, own),
            ),
        )
        if attribute == "default":
            owner.__dict__[_HELD].default = owner.default

    def included(
        self,
        included: Any,  # noqa: ANN401
        prefix: str,
        *,
        top: bool,
    ) -> None:
        """Hold an include, gating each list of contexts FastAPI builds for it later.

        A mount it sits under brings a `top` include up to date before its
        open door lets a request through.
        """
        held, new = self._hold(included, prefix)
        if new:
            held.router = fastapi.router_of(included)
            fastapi.watch(
                included,
                top=top,
                changed=functools.partial(_regate_candidates, held),
            )
        if top:
            self._beneath(
                included,
                functools.partial(fastapi.candidates, included, top=True),
            )

    def contextual(
        self,
        route: Any,  # noqa: ANN401
        context: Any,  # noqa: ANN401
        declaration: RouteDeclaration,
    ) -> None:
        """Gate a FastAPI route where it is dispatched, directly or through `context`.

        The route dispatches each request to the gated handler of the
        context FastAPI routed it through, or of the route itself. A
        refusal names the route by its own path, under the root path the
        request arrived at, as the access log does.
        """
        held, new = self._hold(route, "")
        if new:
            route.handle = _chosen(route, held)
        owner = route if context is None else context
        owner.__dict__[_TARGET] = self._gated(
            owner.__dict__.get(_TARGET, held.handle),
            [declaration],
            functools.partial(template_under_root, _own_path(route, context)),
        )


def gate_routes(app: Starlette, gate: Gate) -> None:
    """Wrap every route of `app` with its gate, and gate what lands later.

    The app refuses to build its middleware stack, and so to start, once
    its router is not the one gated here.

    Raises:
        RuntimeError: When the app builds its stack with another router.
    """
    router = app.router
    _walk_router(router, "", _Gating([gate]), frozenset())
    build = app.build_middleware_stack

    def build_middleware_stack() -> ASGIApp:
        if app.router is not router:
            msg = (
                "The app's router was replaced after micro.install(app), so "
                "its routes carry no authentication gate. Call "
                "micro.install(app) once the router is the one the app "
                "serves with."
            )
            raise RuntimeError(msg)
        return build()

    app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]


def _path_under_mounts(own: str, scope: Scope) -> str:
    """Return a route by its own path, under the mounts the request came through."""
    return f"{scope.get(_PREFIX_KEY, '')}{own}" or "/"


_UNDER_MOUNTS: Final = functools.partial(_path_under_mounts, "")
"""Names a mount's refusal by the mounts the request came through."""


def _regate(owner: Any, held: _Held) -> None:  # noqa: ANN401
    """Gate again what `owner` holds now, at every path it sits under."""
    gating = _Gating(held.gates, held.within)
    for prefix in tuple(held.prefixes):
        if isinstance(owner, Router):
            _walk_router(owner, prefix, gating, frozenset())
        else:
            _walk_route(owner, prefix, gating, frozenset(), None)
    gating.settle()


def _refresh(router: Router, held: _Held) -> None:
    """Gate again what a router holds, once its route list or its default is not the one gated.

    A route taken out of the list settles the doors of the mounts it sits
    under.
    """
    if router.routes is not held.routes or router.default is not held.default:
        _regate(router, held)
    elif len(router.routes) != held.size:
        held.size = len(router.routes)
        _Gating(held.gates, held.within).settle()


def _guarded_router(router: Router, held: _Held, stack: ASGIApp) -> ASGIApp:
    """Return `stack`, run once the router's route list and default are the ones gated."""

    def guarded(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        _refresh(router, held)
        return stack(scope, receive, send)

    return guarded


def _guarded_mount(mount: Mount | Host, held: _Held) -> ASGIApp:
    """Return what the mount dispatches to, once its app is the one gated.

    An open door brings what is under the mount up to date first, so it
    shuts once no route under it is anonymous. A mount adds its path to
    the ones the request came through, which a refusal names its route by.
    """
    own = mount.path if isinstance(mount, Mount) else ""

    def guarded(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        if mount.app is not held.app:
            _regate(mount, held)
            return mount.handle(scope, receive, send)
        if held.shut is not None and held.door is held.handle:
            for refresh in tuple(held.beneath.values()):
                refresh()
        if own:
            scope[_PREFIX_KEY] = f"{scope.get(_PREFIX_KEY, '')}{own}"
        return held.door(scope, receive, send)

    return guarded


def _regate_candidates(held: _Held, found: list[Any]) -> None:
    """Gate the contexts FastAPI built anew for an include, at every path it sits under."""
    gating = _Gating(held.gates, held.within)
    for prefix in tuple(held.prefixes):
        for candidate in found:
            _walk_candidate(candidate, prefix, gating, frozenset(), held.router)
    gating.settle()


def _chosen(route: Any, held: _Held) -> ASGIApp:  # noqa: ANN401
    """Return what a FastAPI route dispatches to: the gated handler of the request's context.

    That of the context FastAPI routed the request through, or of the
    route itself. One neither was gated for, reached from an app no walk
    gated, is gated where it is met, with no path above its own.
    """
    context_of = fastapi.context_of

    def chosen(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        context = context_of(scope, route)
        owner = route if context is None else context
        target = owner.__dict__.get(_TARGET)
        if target is None:
            path = _own_path(route, context) or "/"
            _Gating(held.gates, held.within).contextual(
                route, context, _dependant_declaration(owner, route, path, None)
            )
            target = owner.__dict__[_TARGET]
        return target(scope, receive, send)

    return chosen


def _own_path(route: Any, context: Any) -> str:  # noqa: ANN401
    """Return a FastAPI route's own path, under the include `context` dispatches it through."""
    return (
        getattr(route, "path", "")
        if context is None
        else fastapi.path_of(context)
    )


def _own_routes(router: Router) -> _GatedRoutes:
    """Return the router's route list, made one that gates what lands in it."""
    routes = router.routes
    if isinstance(routes, _GatedRoutes) and routes.owner is router:
        return routes
    owned = _GatedRoutes(routes, router)
    router.routes = owned
    return owned


class _GatedRoutes(list[Any]):
    """A router's routes, gating every route put into the list."""

    __slots__ = ("owner",)

    def __init__(self, routes: Iterable[Any], owner: Router) -> None:
        """Hold `routes` for the router `owner`, gating none of them yet."""
        super().__init__(routes)
        self.owner = owner

    def landed(self, routes: Iterable[Any]) -> None:
        """Gate `routes` at every path the router sits under."""
        owner = self.owner
        held: _Held = owner.__dict__[_HELD]
        gating = _Gating(held.gates, held.within)
        for prefix in tuple(held.prefixes):
            for route in routes:
                _walk_route(
                    route, prefix, gating, frozenset({id(owner)}), owner
                )
        if self is held.routes:
            held.size = len(self)
        gating.settle()

    def append(self, route: Any) -> None:  # noqa: ANN401
        """Add a route at the end, gated."""
        super().append(route)
        self.landed((route,))

    def insert(self, index: SupportsIndex, route: Any) -> None:  # noqa: ANN401
        """Add a route at `index`, gated."""
        super().insert(index, route)
        self.landed((route,))

    def extend(self, routes: Iterable[Any]) -> None:
        """Add routes at the end, gated."""
        added = list(routes)
        super().extend(added)
        self.landed(added)

    def __iadd__(self, routes: Iterable[Any]) -> Self:  # type: ignore[misc]
        """Add routes at the end, gated."""
        added = list(routes)
        super().__iadd__(added)
        self.landed(added)
        return self

    def __setitem__(self, index: Any, value: Any) -> None:  # noqa: ANN401
        """Put a route, or routes for a slice, in place, gated."""
        added = list(value) if isinstance(index, slice) else [value]
        super().__setitem__(index, added if isinstance(index, slice) else value)
        self.landed(added)
