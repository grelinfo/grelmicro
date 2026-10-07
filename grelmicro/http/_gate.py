"""The gate each route dispatches through, decided once the framework routed.

An integration hands `gate(app, *declarations)` what its router dispatches
to, and dispatches to what it returns. The returned app picks the
declaration by method, refuses a request the route does not admit, and
serves the one it admits.

Terms used here:

- The edge of an app is where its authentication hands a request on to
  its router. The app's answering middleware, which may answer a request
  themselves (the rate limit, the response cache, idempotency and
  conditional requests), are skipped there.
- A lane is those answering middleware, built once for one route, which a
  route's gate runs around the route once it admitted the request. It
  shows them the request as each edge saw it, and hands the route the
  request as its router left it.
- A door is the gate at the entrance of a subtree whose routes carry
  gates of their own. It refuses as a route's gate does, and runs no lane.
"""

from __future__ import annotations

import functools
from collections import Counter
from typing import TYPE_CHECKING, Any, Final, NamedTuple, cast

from grelmicro._paths import ROUTE_KEY, route_path, selects
from grelmicro.errors import AuthenticationRequiredError, InsufficientScopeError
from grelmicro.http._component import ErrorResponses, raw_headers_of, send_error
from grelmicro.http._kinds import AUTHENTICATION_REQUIRED, INSUFFICIENT_SCOPE
from grelmicro.http._requirement import TOKEN_SCOPE_KEY, recorded
from grelmicro.http._routes import (
    RouteDeclaration,
    refuse_impossible,
    route_name,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, MutableMapping

    from grelmicro.http._component import RenderedError

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

    Answering = tuple[tuple[type[Any], dict[str, Any]], ...]
    """Each answering middleware, as its class and its arguments, in order."""

    _Check = Callable[[Scope], "_Refusal | None"]
    """What one declaration asks of a request: `None` to serve it, or its refusal."""

    _Admit = Callable[[Scope], "ASGIApp | None"]
    """What a route asks of a request: `None` to serve it, or its named refusal."""

__all__ = [
    "CROSSED_KEY",
    "EXCLUDED_ROOT_KEY",
    "GATE_KEY",
    "PENDING_KEY",
    "ROUTE_KEY",
    "UNANSWERED_KEY",
    "Carried",
    "Crossing",
    "Edge",
    "GatePolicy",
    "RouteGate",
    "Unanswered",
    "answered",
    "crossed",
    "deny_websocket",
    "edge_of",
    "refuse_websocket",
    "wrapped",
]

EXCLUDED_ROOT_KEY: Final = "grelmicro.excluded_root"
"""Where the middleware leaves the root path it matched `exclude` against."""

GATE_KEY: Final = "grelmicro.gate"
"""Where the middleware leaves the `GatePolicy` of the app serving the request."""

_GATED: Final = "__grelmicro_gated__"
"""Set on an app a gate returned."""

_POLICY_VIOLATION: Final = 1008
"""Close code for a websocket refused on a server that cannot send a `401`."""


class GatePolicy:
    """What the gates of one app read off each request, beside the route's own.

    The middleware of the app serving a request puts it on the scope, so a
    route gated for several apps answers each by its own `exclude` and in
    its own error format.
    """

    __slots__ = ("answering", "behind", "defers", "errors", "exclude")

    def __init__(
        self,
        *,
        exclude: tuple[str, ...],
        errors: ErrorResponses,
        answering: Answering = (),
        behind: bool = False,
    ) -> None:
        """Hold the app's `exclude`, its error format and its answering middleware.

        `answering` is each answering middleware in the order
        `micro.install(app)` placed them. `behind` says the app's
        authentication runs behind its router, and checks each route there.
        """
        self.exclude = exclude
        self.errors = errors
        self.answering = answering
        self.behind = behind
        self.defers = False
        """A route of the app serves a caller with no credential, so such a request is routed."""


class Edge:
    """The edge of one app: where its authentication hands a request on.

    `below` is what the request goes on to. `answering` is each of the
    app's answering middleware the request skips there, to run in the
    lane of the route it reaches instead.
    """

    __slots__ = ("_after", "answering", "below", "only")

    def __init__(self, below: ASGIApp, answering: Answering) -> None:
        """Hand requests on to `below`, running `answering` at each route."""
        self.below = below
        self.answering = answering
        self.only: tuple[Edge, ...] = (self,)
        """The edges of a request that crossed this one and no other."""
        self._after: dict[tuple[Edge, ...], tuple[Edge, ...]] = {}

    def after(self, outer: tuple[Edge, ...]) -> tuple[Edge, ...]:
        """Return the edges of a request that crossed `outer`, then this one.

        The same tuple each time for the same `outer`.
        """
        edges = self._after.get(outer)
        if edges is None:
            edges = self._after[outer] = (*outer, self)
        return edges


def edge_of(app: ASGIApp, policy: GatePolicy) -> Edge:
    """Return the edge where an authentication passes requests to `app`.

    Walks down from `app` while each middleware is the next of the
    policy's answering middleware, in the order they were placed. The
    first that is not ends the walk, and everything from it on serves the
    request as the app's own code.

    A middleware skipped there that carries `before_routing(app)` runs
    what that returns at the edge instead, outermost first.
    """
    remaining = list(policy.answering)
    found: list[tuple[type[Any], dict[str, Any]]] = []
    skipped: list[Any] = []
    node: Any = app
    while True:
        index = next(
            (
                index
                for index, (middleware, _) in enumerate(remaining)
                if type(node) is middleware
            ),
            None,
        )
        if index is None:
            break
        found.append(remaining[index])
        skipped.append(node)
        del remaining[: index + 1]
        node = node.app
    for middleware in reversed(skipped):
        before_routing = getattr(middleware, "before_routing", None)
        if before_routing is not None:
            node = before_routing(node)
    return Edge(node, tuple(found))


UNANSWERED_KEY: Final = "grelmicro.unanswered"
"""Where a request routed without a credential keeps its `Unanswered`."""


class Unanswered:
    """Whether no gate admitted or refused a request routed without a credential.

    One object, so every copy of the scope the request is routed with
    shares it.
    """

    __slots__ = ("open",)

    def __init__(self) -> None:
        """Start unanswered."""
        self.open = True


def answered(scope: Scope) -> None:
    """Mark the request as answered, when it was routed without a credential.

    A gate marks what it admitted or refused, and a flood limit what it
    refused before routing, so neither answer becomes the `401` of a
    request nothing answered.
    """
    unanswered = scope.get(UNANSWERED_KEY)
    if unanswered is not None:
        unanswered.open = False


PENDING_KEY: Final = "grelmicro.pending"
"""Where a route's gate leaves its check for an authentication behind the router."""

CROSSED_KEY: Final = "grelmicro.crossed"
"""Where the authentication of each app a request crossed leaves a `Crossing`."""

_ROUTED: Final = "grelmicro.routed"
"""Where a lane keeps the path, root path, app and handler the router set."""

_RAISED: Final = "grelmicro.raised"
"""Where a lane keeps the exception the route itself raised."""

_ROUTED_HANDLER: Final = "route_handler"
"""The key a router writes the handler it matched under, read as routed."""

_UNSET: Final = object()
"""A key the scope did not hold."""


class Crossing(NamedTuple):
    """One edge a request crossed, and the request as it arrived there."""

    edge: Edge
    path: str
    root_path: str
    app: Any
    outer: Crossing | None
    """The crossing of the app the request crossed before this one, if any."""
    edges: tuple[Edge, ...]
    """Every edge the request crossed so far, the outermost first."""


def crossed(scope: Scope, edge: Edge) -> None:
    """Record that the request crossed `edge`, with what it arrived with there.

    A request crossing an edge it already crossed, such as one routed
    through an app mounted inside itself, keeps what it recorded there
    first, so the answering middleware of that app run once.
    """
    outer: Crossing | None = scope.get(CROSSED_KEY)
    if outer is None:
        edges = edge.only
    elif edge in outer.edges:
        return
    else:
        edges = edge.after(outer.edges)
    scope[CROSSED_KEY] = Crossing(
        edge,
        scope["path"],
        scope.get("root_path", ""),
        scope.get("app"),
        outer,
        edges,
    )


class Carried(BaseException):
    """An exception an answering middleware raised in a lane, carried to its edge.

    It is not an `Exception`, so the handlers between the route and the
    edge let it pass, and the authentication at `edge` raises `error`
    again, where the middleware would have raised it before routing.
    """

    def __init__(self, error: Exception, edge: Edge) -> None:
        """Carry `error` up to the authentication at `edge`."""
        super().__init__(error)
        self.error = error
        self.edge = edge


def _lane(target: ASGIApp, edges: tuple[Edge, ...], *, moved: bool) -> ASGIApp:
    """Return the lane of `target` for a request that crossed `edges`.

    The middleware of the outermost app run first, each app's with the
    request as its edge saw it. `moved` says the router changed the path,
    the root path or the app the request carries, which `target` then
    gets back.
    """
    if not any(edge.answering for edge in edges):
        return target
    app = _route(target, restore=moved)
    if not moved:
        return _segment(wrapped(app, edges[0].answering), edges[0])
    last = len(edges) - 1
    for index in range(last, -1, -1):
        edge = edges[index]
        app = _segment(
            _presented(wrapped(app, edge.answering), last - index), edge
        )
    return app


def wrapped(app: ASGIApp, answering: Answering) -> ASGIApp:
    """Return `app` inside the `answering` middleware, the first outermost."""
    for middleware, options in reversed(answering):
        app = middleware(app, **options)
    return app


def _route(target: ASGIApp, *, restore: bool) -> ASGIApp:
    """Return `target`, marking what it raises as the route's own.

    With `restore`, it gets the request as the router left it.
    """

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        if restore:
            (
                scope["path"],
                scope["root_path"],
                scope["app"],
                handler,
            ) = scope.pop(_ROUTED)
            if handler is not _UNSET:
                scope[_ROUTED_HANDLER] = handler
        try:
            await target(scope, receive, send)
        except Exception as error:
            scope[_RAISED] = error
            raise

    return route


def _segment(chain: ASGIApp, edge: Edge) -> ASGIApp:
    """Return `chain`, carrying up to `edge` what its middleware raise.

    An exception the route raised is raised as it is.
    """

    async def segment(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await chain(scope, receive, send)
        except Exception as error:
            if scope.get(_RAISED) is error:
                raise
            raise Carried(error, edge) from None

    return segment


def _presented(chain: ASGIApp, hops: int) -> ASGIApp:
    """Return `chain`, run with the request as it arrived at the edge `hops` apps out.

    What the router set is kept for the route, the first time.
    """

    def presented(
        scope: Scope, receive: Receive, send: Send
    ) -> Awaitable[None]:
        arrived: Crossing = scope[CROSSED_KEY]
        for _ in range(hops):
            arrived = cast("Crossing", arrived.outer)
        if _ROUTED not in scope:
            scope[_ROUTED] = (
                scope["path"],
                scope.get("root_path", ""),
                scope.get("app"),
                scope.pop(_ROUTED_HANDLER, _UNSET),
            )
        scope["path"] = arrived.path
        scope["root_path"] = arrived.root_path
        scope["app"] = arrived.app
        return chain(scope, receive, send)

    return presented


class _Refusal:
    """Refuses a request at a route's gate, in the app's error format.

    An ASGI app, sent in place of the route. `status` says which refusal
    it is: `401` for a request with no credential, `403` for a caller
    lacking a scope.
    """

    __slots__ = ("_error", "_errors", "status", "template")

    def __init__(
        self,
        error: Callable[[], Exception],
        *,
        status: int,
        template: str,
        errors: ErrorResponses,
    ) -> None:
        """Refuse with the error `error` builds, rendered by `errors`."""
        self._error = error
        self._errors = errors
        self.status = status
        self.template = template

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Record the refusal, then answer it."""
        error = self._error()
        scope.setdefault(ROUTE_KEY, self.template)
        recorded(scope, error)
        rendered = cast(
            "RenderedError",
            self._errors.render(error, instance=scope.get("path")),
        )
        if scope["type"] == "http":
            await send_error(send, rendered)
            return
        await deny_websocket(scope, receive, send, rendered)


def _no_credential(template: str, errors: ErrorResponses) -> _Refusal:
    """Return the `401` refusing a request without a credential."""
    return _Refusal(
        AuthenticationRequiredError,
        status=AUTHENTICATION_REQUIRED.status,
        template=template,
        errors=errors,
    )


async def deny_websocket(
    scope: Scope, receive: Receive, send: Send, rendered: RenderedError
) -> None:
    """Refuse a websocket handshake, with the refusal's status when possible.

    The handshake is read first. A server with the denial response
    extension gets the whole response, the `401` and its challenge
    included. Any other server gets the handshake closed before it
    completes, which it answers `403` with no headers.
    """
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    await refuse_websocket(scope, send, rendered)


async def refuse_websocket(
    scope: Scope, send: Send, rendered: RenderedError
) -> None:
    """Refuse a websocket handshake already read, as `deny_websocket` does."""
    if "websocket.http.response" in (scope.get("extensions") or {}):
        await send(
            {
                "type": "websocket.http.response.start",
                "status": rendered.status,
                "headers": raw_headers_of(rendered),
            }
        )
        await send(
            {
                "type": "websocket.http.response.body",
                "body": rendered.body,
                "more_body": False,
            }
        )
        return
    await send({"type": "websocket.close", "code": _POLICY_VIOLATION})


def _gate_path(scope: Scope) -> str:
    """Return the path the app routed the request with, from its own root.

    Read the way the app's router reads it, against the root path the
    request arrived at the middleware with, which a mount inside the app
    grows by its prefix. So the path reads the way `exclude` matched it
    before routing.
    """
    return route_path({**scope, "root_path": scope[EXCLUDED_ROOT_KEY]})


def _check_of(declaration: RouteDeclaration) -> _Check:
    """Return the check one declaration asks for, built once for its route.

    The check reads the app's `GatePolicy` off the request. A request
    `AuthenticatedRequests` verified a token for is served unless it lacks
    a scope. Any other request is served on an anonymous route, or on a
    path its app's middleware excluded that is still in `exclude` once
    routed, and refused `401` otherwise, in the default format when it
    carries no policy.
    """
    if declaration.anonymous:
        return _anonymous(declaration.path)
    unauthenticated = _without_credential(declaration.path)
    if not declaration.scopes:
        return _authenticated(unauthenticated)
    return _scoped(declaration, unauthenticated)


def _anonymous(template: str) -> _Check:
    """Return the check of a route serving a caller with no credential.

    It serves any request carrying the policy of an app, and refuses one
    without.
    """
    unvouched = _no_credential(template, ErrorResponses())

    def check(scope: Scope) -> _Refusal | None:
        if GATE_KEY not in scope:
            return unvouched
        answered(scope)
        return None

    return check


def _without_credential(template: str) -> _Check:
    """Return the check of a request that sent no verified credential.

    It is served only when its app's middleware excluded its path and the
    path it was routed with is still in that app's `exclude`. Without a
    policy it is refused in the default format.
    """
    unvouched = _no_credential(template, ErrorResponses())

    def check(scope: Scope) -> _Refusal | None:
        policy: GatePolicy | None = scope.get(GATE_KEY)
        if policy is None:
            return unvouched
        exclude = policy.exclude
        if (
            exclude
            and EXCLUDED_ROOT_KEY in scope
            and not selects(_gate_path(scope), include=(), exclude=exclude)
        ):
            return None
        return _no_credential(template, policy.errors)

    return check


def _authenticated(unauthenticated: _Check) -> _Check:
    """Return the check of a route requiring a caller and no scope."""

    def check(scope: Scope) -> _Refusal | None:
        if TOKEN_SCOPE_KEY in scope:
            return None
        return unauthenticated(scope)

    return check


def _scoped(declaration: RouteDeclaration, unauthenticated: _Check) -> _Check:
    """Return the check of a route requiring a caller holding every scope.

    The scopes granted are read where `Authenticated` reads them:
    `scope["auth"]` first, then the caller's own for one set without
    credentials there.
    """
    held = declaration.scopes.issubset
    missing = functools.partial(
        InsufficientScopeError, scopes=tuple(sorted(declaration.scopes))
    )
    template = declaration.path

    def check(scope: Scope) -> _Refusal | None:
        if TOKEN_SCOPE_KEY not in scope:
            return unauthenticated(scope)
        granted: Any = getattr(scope.get("auth"), "scopes", None)
        if granted is None:
            granted = getattr(scope.get("user"), "scopes", ())
        if held(granted):
            return None
        policy: GatePolicy | None = scope.get(GATE_KEY)
        return _Refusal(
            missing,
            status=INSUFFICIENT_SCOPE.status,
            template=template,
            errors=ErrorResponses() if policy is None else policy.errors,
        )

    return check


def _admission(
    declarations: tuple[RouteDeclaration, ...],
    name: Callable[[Scope], str] | None,
) -> _Admit:
    """Return what the route asks of a request, by the request's method.

    A method no declaration names is checked as an authenticated route.
    A refusal is named by `name`, when there is one, and answers a request
    routed without a credential.
    """
    table: dict[str | None, _Check] = {}
    every: _Check | None = None
    for declaration in declarations:
        check = _check_of(declaration)
        if declaration.methods is None:
            every = check
        else:
            table.update(dict.fromkeys(declaration.methods, check))
    if every is None:
        every = _check_of(RouteDeclaration(declarations[0].path))
    pick = table.get
    otherwise = every

    def admit(scope: Scope) -> ASGIApp | None:
        refusal = pick(scope.get("method"), otherwise)(scope)
        if refusal is None:
            return None
        answered(scope)
        if name is not None:
            scope[ROUTE_KEY] = name(scope)
        return refusal

    return admit


def _refuse_overlap(declarations: tuple[RouteDeclaration, ...]) -> None:
    """Refuse declarations that answer one request twice.

    Raises:
        TypeError: If there is no declaration.
        ValueError: If two declarations name the same method, or one names
            every method beside another.
    """
    if not declarations:
        msg = (
            "gate(app) was handed no declaration, so nothing says what the "
            "route requires. Pass the route's RouteDeclaration, or one per "
            "method set."
        )
        raise TypeError(msg)
    if len(declarations) == 1:
        return
    if any(declaration.methods is None for declaration in declarations):
        twice = ["every method"]
    else:
        counted = Counter(
            method
            for declaration in declarations
            for method in declaration.methods or ()
        )
        twice = sorted(method for method, count in counted.items() if count > 1)
    if twice:
        route = route_name(declarations[0])
        msg = (
            f"{route} is declared twice for {', '.join(twice)}, so the gate "
            f"cannot tell which one a request meets. Declare each method "
            f"once."
        )
        raise ValueError(msg)


def _gated(target: ASGIApp, admit: _Admit, *, door: bool) -> ASGIApp:
    """Return `target`, run once `admit` lets the request through.

    It returns the awaitable `target`, its lane, or the refusal. A door
    runs no lane.
    """
    lanes: tuple[
        dict[tuple[Edge, ...], ASGIApp], dict[tuple[Edge, ...], ASGIApp]
    ] = ({}, {})

    def lane(scope: Scope, arrived: Crossing) -> ASGIApp:
        moved = (
            arrived.outer is not None
            or scope["path"] != arrived.path
            or scope.get("app") is not arrived.app
            or scope.get("root_path", "") != arrived.root_path
        )
        built = lanes[moved].get(arrived.edges)
        if built is None:
            built = lanes[moved][arrived.edges] = _lane(
                target, arrived.edges, moved=moved
            )
        return built

    def gated(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        refusal = admit(scope)
        if refusal is not None:
            return refusal(scope, receive, send)
        arrived = scope.get(CROSSED_KEY)
        if arrived is None or door:
            return target(scope, receive, send)
        return lane(scope, arrived)(scope, receive, send)

    setattr(gated, _GATED, True)
    return gated


def _pending(target: ASGIApp, admit: _Admit) -> ASGIApp:
    """Return `target`, leaving `admit` for the authentication behind the router."""

    def gated(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        scope[PENDING_KEY] = admit
        return target(scope, receive, send)

    setattr(gated, _GATED, True)
    return gated


class RouteGate:
    """Wraps what each route of one app dispatches to with its gate.

    `micro.install(app)` builds one from `AuthenticatedRequests` and passes
    it to the integration's `install_route_gate(app, gate)`. Each call
    refuses a declaration that cannot hold, and counts it as gated, so a
    route the integration lists without a gate is found. The app it
    returns holds no policy of an app, and reads the policy of the app
    serving the request off the request.
    """

    __slots__ = ("_app", "_gated", "_listing", "_public", "policy")

    def __init__(
        self,
        app: Any,  # noqa: ANN401
        *,
        exclude: tuple[str, ...],
        errors: ErrorResponses,
        public: Any,  # noqa: ANN401
        answering: Answering = (),
        behind: bool = False,
    ) -> None:
        """Gate the routes of `app`, refusing in the format of `errors`.

        `answering` is each answering middleware of the app, which run in
        each route's lane once its gate admitted the request. `behind`
        says the app's authentication runs behind its router, so each
        route leaves its check for it.
        """
        self._app = app
        self._public = public
        self.policy = GatePolicy(
            exclude=exclude, errors=errors, answering=answering, behind=behind
        )
        self._gated: Counter[RouteDeclaration] = Counter()
        self._listing: Callable[[], Iterable[RouteDeclaration]] | None = None

    def __call__(
        self,
        app: ASGIApp,
        *declarations: RouteDeclaration,
        name: Callable[[Scope], str] | None = None,
        door: bool = False,
    ) -> ASGIApp:
        """Return the app to dispatch to in place of `app`.

        It serves the request once the declaration of its method admits
        it, in the route's lane. A method no declaration names needs an
        authenticated caller. A refusal is named by `name`, called with the
        request's scope, or by the declaration's path. With `door`, `app`
        is the entrance of a subtree whose routes carry gates of their
        own, and runs no lane. An app this returned already is returned as
        it is, and its declarations are counted.

        Raises:
            TypeError: If there is no declaration, or a `cache` is neither
                a boolean nor a `timedelta`.
            ValueError: If a declaration cannot hold, or two declarations
                answer the same method, naming the route.
        """
        for declaration in declarations:
            refuse_impossible(declaration)
        _refuse_overlap(declarations)
        self._gated.update(declarations)
        if any(declaration.anonymous for declaration in declarations):
            self.policy.defers = True
        if getattr(app, _GATED, False):
            return app
        admit = _admission(declarations, name)
        if self.policy.behind:
            return _pending(app, admit)
        return _gated(app, admit, door=door)

    def hold(
        self,
        *,
        wired: bool,
        listing: Callable[[], Iterable[RouteDeclaration]] | None,
    ) -> None:
        """Take the app's routes as gated, once the integration wired them.

        The middleware in front of an app whose routes are `wired` puts its
        policy on each request it lets through. `listing` returns the
        routes the integration declares, each of which must carry a gate.

        Raises:
            RuntimeError: If a listed route carries no gate, naming it.
        """
        self._listing = listing
        self.refuse_ungated()
        if wired:
            self._public.gate(self._app, self.policy)

    def refuse_ungated(self) -> None:
        """Refuse a route the integration lists that carries no gate.

        Raises:
            RuntimeError: Naming the first such route.
        """
        listing = self._listing
        if listing is None:
            return
        ungated = Counter(listing()) - self._gated
        if not ungated:
            return
        route = min(ungated, key=route_name)
        msg = (
            f"{route_name(route)} carries no authentication gate, so a "
            f"request would reach it unchecked. Add a route through the app "
            f"or its router, which gates it as it lands."
        )
        raise RuntimeError(msg)
