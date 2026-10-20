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
- An open gate is the gate of an app without authentication. It refuses
  nothing, and carries the route's declaration into the lane of the
  response cache, idempotency and conditional requests, whose middleware
  hand each request on at their own edge in front of the router.
"""

from __future__ import annotations

import functools
from collections import Counter
from dataclasses import replace
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    NamedTuple,
    cast,
)

from typing_extensions import Doc

from grelmicro import _paths
from grelmicro._paths import (
    ROUTE_KEY,
    path_format,
    route_path,
    selects,
)
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
    "DECLARATION_KEY",
    "EXCLUDED_ROOT_KEY",
    "GATE_KEY",
    "PENDING_KEY",
    "ROUTE_KEY",
    "UNANSWERED_KEY",
    "AnswersAtRoute",
    "AtRoute",
    "Carried",
    "Crossing",
    "Edge",
    "GatePolicy",
    "OpenGate",
    "RouteGate",
    "Unanswered",
    "answered",
    "crossed",
    "deny_websocket",
    "edge_at",
    "edge_of",
    "hand_on",
    "handed_on",
    "refuse_websocket",
    "wrapped",
]

EXCLUDED_ROOT_KEY: Final = "grelmicro.excluded_root"
"""Where the middleware leaves the root path it matched `exclude` against."""

GATE_KEY: Final = "grelmicro.gate"
"""Where the middleware leaves the `GatePolicy` of the app serving the request."""

DECLARATION_KEY: Final = "grelmicro.declaration"
"""Where a route's gate leaves the declaration that admitted the request."""

_GATED: Final = "__grelmicro_gated__"
"""Set on an app a gate returned."""

_OPENED: Final = "__grelmicro_opened__"
"""Set on an app an open gate returned, holding the app it wraps."""

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
    """Where a request is handed on to the router of one app.

    `answering` is each answering middleware the request skips there, to
    run in the lane of the route it reaches instead. An app's
    authentication hands it on at one edge. On an app without
    authentication, the response cache, idempotency and conditional
    requests each hand it on at an edge of their own.
    """

    __slots__ = ("_after", "answering", "only")

    def __init__(self, answering: Answering) -> None:
        """Run `answering` at each route a request handed on here reaches."""
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


def edge_of(app: ASGIApp, policy: GatePolicy) -> tuple[ASGIApp, Edge]:
    """Return what an authentication passes requests on to, and the edge it is at.

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
    return node, Edge(tuple(found))


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
"""Where a lane keeps the path, root path, app and the `ROUTED_KEYS` the router set."""

_RAISED: Final = "grelmicro.raised"
"""Where a lane keeps the exception the route itself raised."""


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


async def hand_on(
    app: ASGIApp, edge: Edge, scope: Scope, receive: Receive, send: Send
) -> None:
    """Hand a request on to `app` at `edge`, for the route it reaches to answer.

    What the middleware of `edge` raise at that route is raised here,
    where they would have raised it before routing.
    """
    crossed(scope, edge)
    try:
        await app(scope, receive, send)
    except Carried as carried:
        if carried.edge is not edge:
            raise
        raised = carried.error
    else:
        return
    raise raised


def _lane(target: ASGIApp, edges: tuple[Edge, ...], *, moved: bool) -> ASGIApp:
    """Return the lane of `target` for a request that crossed `edges`.

    The middleware of the outermost app run first, each app's with the
    request as its edge saw it. `moved` says the router changed the path,
    the root path or the app the request carries, which `target` then
    gets back.
    """
    if not any(edge.answering for edge in edges):
        return handed_on(target)
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


def handed_on(target: ASGIApp) -> ASGIApp:
    """Return `target`, run without the declaration the route's gate left.

    A middleware further in, such as one of a mounted app, reads no
    declaration of the route that admitted the request.
    """

    def route(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        scope.pop(DECLARATION_KEY, None)
        return target(scope, receive, send)

    return route


def _route(target: ASGIApp, *, restore: bool) -> ASGIApp:
    """Return `target`, marking what it raises as the route's own.

    It runs without the declaration the route's gate left. With
    `restore`, it gets the request as the router left it.
    """

    async def route(scope: Scope, receive: Receive, send: Send) -> None:
        scope.pop(DECLARATION_KEY, None)
        if restore:
            (
                scope["path"],
                scope["root_path"],
                scope["app"],
                written,
            ) = scope.pop(_ROUTED)
            scope.update(written)
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

    What the router set is kept for the route, the first time. The
    `ROUTED_KEYS` it wrote are kept with it, so the edge's middleware read
    the request as before routing.
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
                {
                    key: scope.pop(key)
                    for key in _paths.ROUTED_KEYS
                    if key in scope
                },
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
    A request it admits carries the declaration that admitted it. A
    refusal is named by `name`, when there is one, and answers a request
    routed without a credential.
    """
    table: dict[str | None, tuple[_Check, RouteDeclaration]] = {}
    every: tuple[_Check, RouteDeclaration] | None = None
    for declaration in declarations:
        entry = (_check_of(declaration), declaration)
        if declaration.methods is None:
            every = entry
        else:
            table.update(dict.fromkeys(declaration.methods, entry))
    if every is None:
        authenticated = RouteDeclaration(declarations[0].path)
        every = (_check_of(authenticated), authenticated)
    pick = table.get
    otherwise = every

    def admit(scope: Scope) -> ASGIApp | None:
        check, declaration = pick(scope.get("method"), otherwise)
        refusal = check(scope)
        if refusal is None:
            scope[DECLARATION_KEY] = declaration
            return None
        answered(scope)
        if name is not None:
            scope[ROUTE_KEY] = name(scope)
        return refusal

    return admit


def _refuse_excluded_scopes(
    declaration: RouteDeclaration, exclude: tuple[str, ...]
) -> None:
    """Refuse a declaration requiring scopes on a path in `exclude`.

    The gate lets every request on that path through unchecked, so no
    scope it declares is ever checked. The route is named by its path
    with each converter left out.

    Raises:
        ValueError: Naming the route and its scopes.
    """
    if not declaration.scopes or not exclude:
        return
    path = path_format(declaration.path)
    if selects(path, include=(), exclude=exclude):
        return
    route = route_name(replace(declaration, path=path))
    msg = (
        f"{route} is in exclude, so a token is never read there, and "
        f"declares the scopes {' '.join(sorted(declaration.scopes))}, which no "
        f"request there is checked for. Take the path out of exclude, or "
        f"drop the scopes."
    )
    raise ValueError(msg)


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


def _lanes(target: ASGIApp) -> Callable[[Scope, Crossing], ASGIApp]:
    """Return the lane of `target` for a request, built once per edges crossed.

    A request moved when it crossed more than one edge, or the router
    changed the path, the root path or the app it carries since its edge.
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

    return lane


def _gated(target: ASGIApp, admit: _Admit, *, door: bool) -> ASGIApp:
    """Return `target`, run once `admit` lets the request through.

    It returns the awaitable `target`, its lane, or the refusal. A door
    runs no lane.
    """
    lane = _lanes(target)

    def gated(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        refusal = admit(scope)
        if refusal is not None:
            return refusal(scope, receive, send)
        arrived = scope.get(CROSSED_KEY)
        if arrived is None or door:
            del scope[DECLARATION_KEY]
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


class _Counted:
    """Counts the declarations a gate wrapped, to find a listed route it did not.

    `_UNGATED` is the refusal of such a route, formatted with its name.
    """

    __slots__ = ("_gated", "_listing")

    _UNGATED: ClassVar[str]

    def __init__(self) -> None:
        """Count no declaration yet."""
        self._gated: Counter[RouteDeclaration] = Counter()
        self._listing: Callable[[], Iterable[RouteDeclaration]] | None = None

    def _count(
        self,
        declarations: tuple[RouteDeclaration, ...],
        check: Callable[[RouteDeclaration], None] | None = None,
    ) -> None:
        """Refuse declarations that cannot hold, then count them as gated.

        `check` refuses what this gate cannot hold beside it.

        Raises:
            TypeError: If there is no declaration, or a `cache` is neither
                a boolean nor a `timedelta`.
            ValueError: If a declaration cannot hold, `check` refuses it,
                or two declarations answer the same method, naming the
                route.
        """
        for declaration in declarations:
            refuse_impossible(declaration)
            if check is not None:
                check(declaration)
        _refuse_overlap(declarations)
        self._gated.update(declarations)

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
        route = route_name(min(ungated, key=route_name))
        raise RuntimeError(self._UNGATED.format(route=route))


class RouteGate(_Counted):
    """Wraps what each route of one app dispatches to with its gate.

    `micro.install(app)` builds one from `AuthenticatedRequests` and passes
    it to the integration's `install_route_gate(app, gate)`. Each call
    refuses a declaration that cannot hold, and counts it as gated, so a
    route the integration lists without a gate is found. The app it
    returns holds no policy of an app, and reads the policy of the app
    serving the request off the request.
    """

    __slots__ = ("_app", "_policies", "policy")

    _UNGATED = (
        "{route} carries no authentication gate, so a request would reach "
        "it unchecked. Add a route through the app or its router, which "
        "gates it as it lands."
    )

    def __init__(
        self,
        app: Any,  # noqa: ANN401
        *,
        exclude: tuple[str, ...],
        errors: ErrorResponses,
        policies: Any,  # noqa: ANN401
        answering: Answering = (),
        behind: bool = False,
    ) -> None:
        """Gate the routes of `app`, refusing in the format of `errors`.

        `answering` is each answering middleware of the app, which run in
        each route's lane once its gate admitted the request. `behind`
        says the app's authentication runs behind its router, so each
        route leaves its check for it.
        """
        super().__init__()
        self._app = app
        self._policies = policies
        self.policy = GatePolicy(
            exclude=exclude, errors=errors, answering=answering, behind=behind
        )

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
        it is, and its declarations are counted. An app an open gate
        returned is gated in its place, so a route an app without
        authentication shares stays refused there.

        Raises:
            TypeError: If there is no declaration, or a `cache` is neither
                a boolean nor a `timedelta`.
            ValueError: If a declaration cannot hold, requires scopes on a
                path in `exclude`, or two declarations answer the same
                method, naming the route.
        """
        self._count(
            declarations,
            functools.partial(
                _refuse_excluded_scopes, exclude=self.policy.exclude
            ),
        )
        if any(declaration.anonymous for declaration in declarations):
            self.policy.defers = True
        app = getattr(app, _OPENED, app)
        if getattr(app, _GATED, False):
            return app
        admit = _admission(declarations, name)
        if self.policy.behind:
            return _pending(app, admit)
        return _gated(app, admit, door=door)

    def hold(
        self,
        *,
        listing: Callable[[], Iterable[RouteDeclaration]] | None,
    ) -> None:
        """Take the app's routes as gated, once the integration wired them.

        The middleware in front of the app then puts its policy on each
        request it lets through. `listing` returns the routes the
        integration declares, each of which must carry a gate.

        Raises:
            RuntimeError: If a listed route carries no gate, naming it.
        """
        self._listing = listing
        self.refuse_ungated()
        self._policies.gate(self._app, self.policy)


def _carrying(
    declarations: tuple[RouteDeclaration, ...],
) -> Callable[[Scope], RouteDeclaration | None]:
    """Return the declaration of a request's method.

    `None` for a method the route does not take.
    """
    table: dict[str | None, RouteDeclaration] = {}
    every: RouteDeclaration | None = None
    for declaration in declarations:
        if declaration.methods is None:
            every = declaration
        else:
            table.update(dict.fromkeys(declaration.methods, declaration))
    pick = table.get

    def carried(scope: Scope) -> RouteDeclaration | None:
        return pick(scope.get("method"), every)

    return carried


def _opened(
    target: ASGIApp, carried: Callable[[Scope], RouteDeclaration | None]
) -> ASGIApp:
    """Return `target`, run in its lane with the declaration of the request's method.

    A request that crossed no edge, or whose method the route does not
    take, goes straight to `target`.
    """
    lane = _lanes(target)

    def opened(scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        arrived = scope.get(CROSSED_KEY)
        declaration = carried(scope)
        if arrived is None or declaration is None:
            return target(scope, receive, send)
        scope[DECLARATION_KEY] = declaration
        return lane(scope, arrived)(scope, receive, send)

    setattr(opened, _OPENED, target)
    return opened


class OpenGate(_Counted):
    """Wraps what each route of an app without authentication dispatches to.

    `micro.install(app)` builds one when `CachedResponses`,
    `IdempotentRequests` or `ConditionalRequests` is registered and no
    `AuthenticatedRequests` gates the app. It passes the gate to the
    integration's `install_route_gate(app, gate)`.

    The app it returns refuses nothing. It carries the declaration of the
    request's method into the lane of the route, where the response
    cache, idempotency and conditional requests run. Each call refuses a
    declaration that cannot hold, and counts it as gated, so a route the
    integration lists without a gate is found.
    """

    __slots__ = ()

    _UNGATED = (
        "{route} carries no gate, so the response cache, idempotency and "
        "conditional requests never run there. Add a route through the app "
        "or its router, which gates it as it lands."
    )

    def __call__(
        self,
        app: ASGIApp,
        *declarations: RouteDeclaration,
        name: Callable[[Scope], str] | None = None,  # noqa: ARG002
        door: bool = False,
    ) -> ASGIApp:
        """Return the app to dispatch to in place of `app`.

        It serves the request in the route's lane, carrying the
        declaration of its method. A method no declaration names goes
        straight to `app`, which answers it. It refuses nothing, so `name`
        names nothing. With `door`, `app` is the entrance of a subtree
        whose routes carry gates of their own, and is returned as it is.
        An app a gate returned already is returned as it is, and its
        declarations are counted.

        Raises:
            TypeError: If there is no declaration, or a `cache` is neither
                a boolean nor a `timedelta`.
            ValueError: If a declaration cannot hold, or two declarations
                answer the same method, naming the route.
        """
        self._count(declarations)
        if door or getattr(app, _GATED, False) or hasattr(app, _OPENED):
            return app
        return _opened(app, _carrying(declarations))

    def hold(
        self, *, listing: Callable[[], Iterable[RouteDeclaration]] | None
    ) -> None:
        """Take the app's routes as gated, once the integration wired them.

        `listing` returns the routes the integration declares, each of
        which must carry a gate.

        Raises:
            RuntimeError: If a listed route carries no gate, naming it.
        """
        self._listing = listing
        self.refuse_ungated()


class AtRoute:
    """Where one answering middleware runs on an app without authentication.

    It holds the apps whose routes carry an open gate, and the edge the
    middleware hands a request on at there. Its middleware in front of
    such an app hands each request on at that edge, and runs in the lane
    of the route the request reaches instead. `middleware` returns the
    class and the arguments the lane builds it with.
    """

    __slots__ = ("_apps", "_edge", "_middleware")

    def __init__(
        self, middleware: Callable[[], tuple[type[Any], dict[str, Any]]]
    ) -> None:
        """Hold no app yet."""
        self._middleware = middleware
        self._apps: dict[int, tuple[Any, OpenGate]] = {}
        self._edge: Edge | None = None

    def add(self, app: Any, gate: OpenGate) -> None:  # noqa: ANN401
        """Run the middleware at each route of `app`, which `gate` wraps."""
        self._apps[id(app)] = (app, gate)
        if self._edge is None:
            self._edge = Edge((self._middleware(),))

    def edge_for(self, scope: Scope) -> Edge | None:
        """Return the edge a request is handed on at, or `None` to answer it in front.

        `None` for a request to an app whose routes carry no open gate.
        """
        return self._edge if id(scope.get("app")) in self._apps else None

    def refuse_ungated(self) -> None:
        """Refuse a route of any app held here that carries no gate.

        Raises:
            RuntimeError: Naming the first such route.
        """
        for _, gate in self._apps.values():
            gate.refuse_ungated()


def edge_at(routed: AtRoute | None, scope: Scope) -> Edge | None:
    """Return the edge a request is handed on at, or `None` to answer it here.

    `None` too at the route, where the request carries a declaration, and
    for an app whose routes carry no open gate.
    """
    if routed is None or DECLARATION_KEY in scope:
        return None
    return routed.edge_for(scope)


class AnswersAtRoute:
    """A component whose middleware runs at the route on an app without authentication.

    It holds the `AtRoute` its middleware reads, as `_routed`.
    """

    _routed: AtRoute

    def _answer_at_route(
        self,
        app: Annotated[Any, Doc("The application without authentication.")],  # noqa: ANN401
        gate: Annotated[OpenGate, Doc("The gate its routes are wrapped with.")],
    ) -> None:
        """Run the middleware at each route of `app`, behind `gate`.

        `micro.install(app)` calls it on an app whose routes carry no
        authentication gate. The middleware in front of the app then hands
        each request on to the route it reaches.
        """
        self._routed.add(app, gate)
