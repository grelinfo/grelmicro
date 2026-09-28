"""Gate every route a Starlette router dispatches to, and list what each declares.

One walk serves both: `route_declarations` collects what it finds, and
`install_route_gate` wraps each route's `handle` with the check its
declarations ask for, ahead of the route's own `405`.

A route added later is gated as it lands in a router's route list. On each
request a router checks that its route list and its default are the ones
it gated, and a mount or a host that its app is, and gates what replaced
them before dispatching.
"""

from __future__ import annotations

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

from grelmicro.http._authentication import ROUTE_KEY
from grelmicro.http._requirement import declared_scopes
from grelmicro.http._routes import RouteDeclaration

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
    Check = Callable[[Scope], ASGIApp | None]
    Gate = Callable[[RouteDeclaration], Check]

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

_GATED: Final = "__grelmicro_gated__"
"""Set on a `handle` or a `default` once it carries a gate."""

_PREFIX_KEY: Final = "grelmicro.route_prefix"
"""Where each mount a request comes through adds its path, as its route declares it."""

_HELD: Final = "_grelmicro_held"
"""Where a router, a mount or a host keeps what it was gated with."""


class _Visitor(Protocol):
    """What a walk hands each thing it finds."""

    def router(self, router: Router, prefix: str) -> None:
        """Take a router, found under `prefix`, before its routes."""

    def mount(
        self,
        mount: Mount | Host,
        prefix: str,
        whole: RouteDeclaration | None,
    ) -> None:
        """Take a mount or a host, and the declaration it is gated as a whole with."""

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


def _walk_router(
    router: Router, prefix: str, visit: _Visitor, ancestry: frozenset[int]
) -> None:
    """Walk every route of a router, and its default when it has one of its own."""
    if id(router) in ancestry:
        return
    ancestry |= {id(router)}
    visit.router(router, prefix)
    for route in list(router.routes):
        _walk_route(route, prefix, visit, ancestry)
    _walk_default(router, prefix, visit)


def _walk_route(
    route: Any,  # noqa: ANN401
    prefix: str,
    visit: _Visitor,
    ancestry: frozenset[int],
) -> None:
    """Walk one route, and the router behind a mount or a host.

    A mount or a host whose app is not a Starlette router is gated as one
    protected route, and the router found behind the app, if any, is
    walked too.
    """
    if isinstance(route, Mount | Host):
        under = f"{prefix}{route.path}" if isinstance(route, Mount) else prefix
        inner, app = _inner_router(route.app)
        whole = None if inner is route.app else RouteDeclaration(under or "/")
        visit.mount(route, prefix, whole)
        if app is not None:
            visit.app(app, under)
        if inner is not None:
            _walk_router(inner, under, visit, ancestry)
        return
    path = f"{prefix}{getattr(route, 'path', '')}" or "/"
    visit.route(route, "handle", path, _declarations(route, path))


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
        if type(app) is Router:
            return app, None
        if isinstance(app, Starlette):
            router = app.router
            return (router if type(router) is Router else None), app
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


class _Listing:
    """Collects every declaration a walk finds."""

    def __init__(self) -> None:
        """Start with none."""
        self.found: list[RouteDeclaration] = []

    def router(self, router: Router, prefix: str) -> None:
        """List nothing for a router, whose routes are listed on their own."""

    def mount(
        self,
        mount: Mount | Host,  # noqa: ARG002
        prefix: str,  # noqa: ARG002
        whole: RouteDeclaration | None,
    ) -> None:
        """List the declaration a mount is gated as a whole with, if any."""
        if whole is not None:
            self.found.append(whole)

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


def declarations_of(app: Starlette) -> list[RouteDeclaration]:
    """Return what every route of `app` declares, walked as the gates are."""
    listing = _Listing()
    _walk_router(app.router, "", listing, frozenset())
    return listing.found


class _Held:
    """What a router, a mount or a host was gated with, and what it held then."""

    __slots__ = (
        "app",
        "default",
        "gates",
        "handle",
        "prefixes",
        "router",
        "routes",
    )

    def __init__(self, handle: Any) -> None:  # noqa: ANN401
        """Hold nothing yet, keeping the `handle` it dispatched with."""
        self.gates: list[Gate] = []
        self.prefixes: list[str] = []
        self.handle = handle
        self.routes: Any = None
        self.default: Any = None
        self.app: Any = None
        self.router: Any = None


class _Gating:
    """Wraps what each route dispatches to with its checks, and holds each router."""

    def __init__(self, gates: list[Gate]) -> None:
        """Gate with every gate in `gates`, the first building the checks."""
        self.gates = gates

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
        return held, new

    def _checks(self, declarations: list[RouteDeclaration]) -> list[Check]:
        """Hand every declaration to every gate, and return the first gate's checks."""
        first, *others = self.gates
        for gate in others:
            for declaration in declarations:
                gate(declaration)
        return [first(declaration) for declaration in declarations]

    def router(self, router: Router, prefix: str) -> None:
        """Hold `router`, checking its route list and its default on each request."""
        held, new = self._hold(router, prefix)
        held.routes = _own_routes(router)
        held.default = router.default
        if new:
            router.middleware_stack = _guarded_router(  # type: ignore[assignment]
                router, held, router.middleware_stack
            )

    def mount(
        self,
        mount: Mount | Host,
        prefix: str,
        whole: RouteDeclaration | None,
    ) -> None:
        """Hold `mount`, checking its app on each request, gated as a whole if asked."""
        held, _ = self._hold(mount, prefix)
        checks = self._checks([] if whole is None else [whole])
        if held.app is mount.app:
            return
        held.app = mount.app
        mount.handle = _guarded_mount(  # type: ignore[method-assign,assignment]  # ty: ignore[invalid-assignment]
            mount, held, held.handle, checks[0] if checks else None
        )

    def app(self, app: Starlette, prefix: str) -> None:
        """Hold a mounted Starlette app, gating the router it builds its stack with."""
        held, new = self._hold(app, prefix)
        held.router = app.router
        if not new:
            return
        build = app.build_middleware_stack

        def build_middleware_stack() -> ASGIApp:
            if app.router is not held.router:
                gating = _Gating(held.gates)
                for under in tuple(held.prefixes):
                    _walk_router(app.router, under, gating, frozenset())
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
        """Wrap `owner.attribute` with the checks its declarations ask for.

        Each declaration is handed to every gate at every path the route
        sits under, and the route is wrapped once. A method no declaration
        names meets the check of an authenticated route.
        """
        declarations = declarations or [RouteDeclaration(path)]
        checks = self._checks(declarations)
        current = getattr(owner, attribute)
        if not getattr(current, _GATED, False):
            table: dict[str, Check] = {}
            every: Check | None = None
            for declaration, check in zip(declarations, checks, strict=True):
                if declaration.methods is None:
                    every = check
                else:
                    table.update(dict.fromkeys(declaration.methods, check))
            if every is None:
                every = self.gates[0](RouteDeclaration(path))
            own = getattr(owner, "path", "") if attribute == "handle" else ""
            setattr(
                owner,
                attribute,
                _by_method(current, table, every, own)
                if table
                else _gated(current, every, own),
            )
        if attribute == "default":
            owner.__dict__[_HELD].default = owner.default


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


def _refused(
    refusal: ASGIApp, own: str, scope: Scope, receive: Receive, send: Send
) -> Awaitable[None]:
    """Send `refusal`, naming the route by the mounts the request came through."""
    scope[ROUTE_KEY] = f"{scope.get(_PREFIX_KEY, '')}{own}" or "/"
    return refusal(scope, receive, send)


def _gated(target: ASGIApp, check: Check, own: str) -> ASGIApp:
    """Return `target`, run only once `check` lets the request through.

    It returns the awaitable `target` or the refusal returns. A refusal
    names the route by its own path, `own`, under the mounts the request
    came through.
    """

    def gated(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        refusal = check(scope)
        if refusal is None:
            return target(scope, receive, send)
        return _refused(refusal, own, scope, receive, send)

    setattr(gated, _GATED, True)
    return gated


def _by_method(
    target: ASGIApp, table: dict[str, Check], every: Check, own: str
) -> ASGIApp:
    """Return `target`, run once the check for the request's method lets it through.

    It returns the awaitable `target` or the refusal returns, named as
    `_gated` names it.
    """
    checks = table.get

    def gated(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        refusal = checks(scope["method"], every)(scope)
        if refusal is None:
            return target(scope, receive, send)
        return _refused(refusal, own, scope, receive, send)

    setattr(gated, _GATED, True)
    return gated


def _regate(owner: Any, held: _Held) -> None:  # noqa: ANN401
    """Gate again what `owner` holds now, at every path it sits under."""
    gating = _Gating(held.gates)
    for prefix in tuple(held.prefixes):
        if isinstance(owner, Router):
            _walk_router(owner, prefix, gating, frozenset())
        else:
            _walk_route(owner, prefix, gating, frozenset())


def _guarded_router(router: Router, held: _Held, stack: ASGIApp) -> ASGIApp:
    """Return `stack`, run once the router's route list and default are the ones gated."""

    def guarded(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        if (
            router.routes is not held.routes
            or router.default is not held.default
        ):
            _regate(router, held)
        return stack(scope, receive, send)

    return guarded


def _guarded_mount(
    mount: Mount | Host,
    held: _Held,
    handle: ASGIApp,
    check: Check | None,
) -> ASGIApp:
    """Return `handle`, run once the mount's app is the one gated and `check` passes.

    A mount adds its path to the ones the request came through, which a
    refusal names its route by.
    """
    own = mount.path if isinstance(mount, Mount) else ""

    def guarded(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        if mount.app is not held.app:
            _regate(mount, held)
            return mount.handle(scope, receive, send)
        if own:
            scope[_PREFIX_KEY] = f"{scope.get(_PREFIX_KEY, '')}{own}"
        if check is not None:
            refusal = check(scope)
            if refusal is not None:
                return _refused(refusal, "", scope, receive, send)
        return handle(scope, receive, send)

    return guarded


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
        gating = _Gating(held.gates)
        for prefix in tuple(held.prefixes):
            for route in routes:
                _walk_route(route, prefix, gating, frozenset({id(owner)}))

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
