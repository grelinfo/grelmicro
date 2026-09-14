"""Authentication at the HTTP edge."""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final, Self, cast

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._config import build_config
from grelmicro._paths import (
    PathPatterns,
    _is_mount,
    _is_route,
    _route_source,
    as_patterns,
    compile_mount,
    compile_route,
    route_path,
    selects,
    walk_routes,
)
from grelmicro.errors import (
    AmbiguousCredentialsError,
    AuthenticationRequiredError,
    InsufficientScopeError,
)
from grelmicro.http._component import (
    ErrorResponses,
    raw_headers_of,
    send_error,
)
from grelmicro.http._openapi import add_error_schema
from grelmicro.http._ratelimit import bucket_of
from grelmicro.security.bans import ClientBannedError
from grelmicro.security.jwks import SigningKeysUnavailableError
from grelmicro.security.jwt import (
    DiscoveryConfig,
    TokenRejectedError,
    TokenRejectedReason,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, MutableMapping
    from contextlib import AbstractAsyncContextManager
    from re import Pattern
    from types import TracebackType

    from grelmicro.http._component import RenderedError
    from grelmicro.security.bans import ClientBans
    from grelmicro.security.clientip import TrustedProxies
    from grelmicro.security.jwt import JWTClaims, TokenVerifier

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "AuthenticatedRequests",
    "AuthenticatedRequestsConfig",
    "AuthenticatedRequestsMiddleware",
]

_BEARER = "bearer"
"""The scheme a bearer token travels under, lowercased for the comparison."""

_POLICY_VIOLATION = 1008
"""Close code for a websocket refused on a server that cannot send a `401`."""

_REFUSALS = (
    AmbiguousCredentialsError,
    AuthenticationRequiredError,
    ClientBannedError,
    SigningKeysUnavailableError,
    TokenRejectedError,
)
"""What the middleware answers itself, rather than letting reach the app."""

_ANONYMOUS_MARKER = "__grelmicro_anonymous__"
"""Set on the callable a route declares to be served without a credential."""

ANONYMOUS_OPT = "exclude_from_auth"
"""The `opt` key a Litestar handler declares itself public under.

The key Litestar's own authentication middleware reads, so a handler
written for it is public here too.
"""


async def _anonymous_route() -> None:
    """Declare that this route is served without a credential.

    Async so the framework resolves it on the event loop. It computes
    nothing: what it carries is where it is declared.
    """


setattr(_anonymous_route, _ANONYMOUS_MARKER, True)


def declare_anonymous() -> Callable[[], Awaitable[None]]:
    """Return the callable a route declares to be served without a credential.

    `grelmicro.integrations.fastapi.Anonymous` wraps it in a `Depends`, and
    `micro.install(app)` reads it back off the dependency tree. One object
    for every route, so finding it is a matter of identity.
    """
    return _anonymous_route


@dataclass(frozen=True, slots=True)
class _AnonymousCaller:
    """The caller of a request served without a credential.

    Put in `scope["user"]` and `scope["auth"]` on a path that is not
    authenticated, so `request.user` answers there too rather than failing
    for the want of a middleware that ran and chose not to act.
    """

    subject: None = None
    issuer: None = None
    scopes: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = MappingProxyType({})
    is_authenticated: bool = False
    identity: str = ""
    display_name: str = ""


_ANONYMOUS = _AnonymousCaller()
"""The one anonymous caller, shared by every request served without one."""


_HTTP: Final = frozenset({"http"})
"""The scope type an HTTP route answers."""

_WEBSOCKET: Final = frozenset({"websocket"})
"""The scope type a websocket route answers."""

_EITHER: Final = frozenset({"http", "websocket"})
"""The scope types a mounted application may answer."""


@dataclass(frozen=True, slots=True)
class _Reach:
    """Where one route answers: its scope types, its methods and its paths."""

    kinds: frozenset[str]
    methods: frozenset[str] | None
    pattern: Pattern[str]
    within: frozenset[int] = frozenset()
    """The mounts and hosts the route sits in, which route a request to it."""
    mounts: tuple[Pattern[str], ...] = ()
    """The mounts above the route, outermost first, each matched on its own."""

    def answers(self, kind: str, method: str | None, path: str) -> bool:
        """Return whether this route could answer the request."""
        return (
            kind in self.kinds
            and (self.methods is None or method in self.methods)
            and self.reaches(path)
        )

    def reaches(self, path: str) -> bool:
        """Return whether the URL gets to this route, whatever its method.

        Each mount above it matches on its own and hands on what follows its
        path, as Starlette does, so a converter that spans segments splits
        the URL where the framework splits it.
        """
        for mount in self.mounts:
            matched = mount.fullmatch(path)
            if matched is None:
                return False
            path = f"/{matched.group('path')}"
        return self.pattern.fullmatch(path) is not None


@dataclass(frozen=True, slots=True)
class _Routes:
    """An app's public routes, and every other route that could answer.

    The others are kept by how many segments their path has, so a request is
    held against the ones that could fit it and the few that fit any depth.
    """

    public: tuple[_Reach, ...] = ()
    by_depth: Mapping[int, tuple[_Reach, ...]] = MappingProxyType({})
    anywhere: tuple[_Reach, ...] = ()
    litestar: Any = None
    declared: Mapping[tuple[int, str], tuple[_Reach, str]] = MappingProxyType(
        {}
    )
    nodes: tuple[tuple[int, _Reach], ...] = ()
    redirects: bool = False

    def serves(self, kind: str, method: str | None, path: str) -> bool:
        """Return whether a public route answers the URL and no other could."""
        return any(
            reach.answers(kind, method, path)
            and self._unrivalled(reach, kind, method, path)
            for reach in self.public
        )

    def _unrivalled(
        self, reach: _Reach, kind: str, method: str | None, path: str
    ) -> bool:
        """Return whether nothing but what routes to `reach` answers the URL.

        A route that is not public could answer it, and so could a mount or
        a host the public route does not sit in, which answers every path
        under it whatever routes it holds.
        """
        leaves = (*self.by_depth.get(path.count("/"), ()), *self.anywhere)
        if any(leaf.answers(kind, method, path) for leaf in leaves):
            return False
        return not any(
            node.answers(kind, method, path)
            for owner, node in self.nodes
            if owner not in reach.within
        )

    def routed(self, path: str) -> bool:
        """Return whether anything matches the path over HTTP, whatever the method."""
        reaches = (
            *self.public,
            *self.by_depth.get(path.count("/"), ()),
            *self.anywhere,
            *(node for _, node in self.nodes),
        )
        return any(
            "http" in reach.kinds and reach.reaches(path) for reach in reaches
        )

    def serves_publicly(
        self,
        route: Any,  # noqa: ANN401
        method: str,
        prefix: str,
    ) -> bool:
        """Return whether the middleware serves this route without a credential.

        For a report or a schema, which describe a route rather than a URL.
        The route is tried with a URL of its own whose parameters avoid the
        literal paths other routes are declared with, so a route another one
        covers is described as authenticated, as the middleware treats it.
        `prefix` is the path of the mounts and routers above it, because one
        route can be included under several.
        """
        if self.litestar is not None:
            return _litestar_declares_public(route, method)
        entry = self.declared.get((id(route), prefix))
        if entry is None:
            return False
        reach, template = entry
        sample = _sample_url(template)
        if sample is None:
            return False
        kind = next(iter(reach.kinds))
        return reach.answers(kind, method, sample) and self._unrivalled(
            reach, kind, method, sample
        )


class _PublicRoutes:
    """The routes that declared `Anonymous()`, read off the app.

    Read off each app `micro.install(app)` adds the middleware to, and again
    when the app starts, so a route declared between the two counts as well.

    A public route's path can fit a URL another route answers, and which one
    serves it depends on the order the routes were declared in, or on the
    framework's own preferences. A request is served without a credential
    only when no route that is not public could answer it, so a declaration
    never opens a route it was not written on.
    """

    def __init__(self) -> None:
        """Start with no app and no public route."""
        self._apps: dict[Any, _Routes] = {}

    @property
    def apps(self) -> tuple[Any, ...]:
        """Return every app read so far."""
        return tuple(self._apps)

    def read(self, app: Any) -> None:  # noqa: ANN401
        """Read every route `app` declares, public or not."""
        self._apps[app] = routes_of(app)

    def reread(self) -> None:
        """Read every app again, for the routes declared since install."""
        for app in self._apps:
            self._apps[app] = routes_of(app)

    def matches(self, scope: Scope) -> bool:
        """Return whether the request is served by a route declared public.

        Held against the routes of the app serving it, which the framework
        names in `scope["app"]`, so one registration installed on two apps
        serves each by its own routes.
        """
        routes = self._apps.get(scope.get("app"))
        if routes is None:
            return False
        if routes.litestar is not None:
            return _litestar_serves_publicly(routes.litestar, scope)
        if not routes.public:
            return False
        kind = scope["type"]
        path = route_path(scope)
        if routes.serves(kind, scope.get("method"), path):
            return True
        if kind != "http" or path == "/" or not routes.redirects:
            return False
        # Starlette redirects a path no route matches to the same path with
        # its trailing slash added or removed, when a route matches that one.
        toggled = path.rstrip("/") if path.endswith("/") else f"{path}/"
        return routes.serves(kind, scope["method"], toggled) and not (
            routes.routed(path)
        )


def routes_of(app: Any) -> _Routes:  # noqa: ANN401
    """Read an app's routes, the way its framework declares them."""
    if getattr(app, "asgi_router", None) is not None:
        return _litestar_routes(app)
    return _starlette_routes(app)


def serves_anonymous_routes(app: Any) -> bool:  # noqa: ANN401
    """Return whether the authentication in front of `app` reads `Anonymous()`.

    One `micro.install(app)` added does. One the app added itself serves
    public paths through `exclude` alone, and `install` adds none beside it.
    An app carrying neither is described as `install` would wire it.
    """
    entries = getattr(app, "user_middleware", None)
    if entries is None:
        entries = getattr(app, "middleware", None) or ()
    for entry in entries:
        cls = getattr(entry, "cls", None) or getattr(entry, "middleware", None)
        if isinstance(cls, type) and issubclass(
            cls, AuthenticatedRequestsMiddleware
        ):
            return getattr(entry, "kwargs", {}).get("public") is not None
    return True


def _starlette_routes(app: Any) -> _Routes:  # noqa: ANN401
    """Read a Starlette or FastAPI app's public routes, and all the others.

    Each path is compiled with the framework's own compiler, so it fits
    exactly the URLs the router matches it against. A mount or a host
    answers every path under it, its router's default included, so it
    counts as a route that could answer each of them, except for the
    public routes it holds.
    """
    from starlette.routing import WebSocketRoute  # noqa: PLC0415

    tree = _Tree()
    _read_tree(app, tree)
    public: list[_Reach] = []
    declared: dict[tuple[int, str], tuple[_Reach, str]] = {}
    rivals: list[tuple[str, _Reach]] = []
    for prefix, route, contexts in walk_routes(app, unwrap_middleware=True):
        template = f"{prefix}{route.path}"
        compiled = compile_route(template)
        if isinstance(route, WebSocketRoute):
            reach = _Reach(_WEBSOCKET, None, compiled)
        else:
            methods = getattr(route, "methods", None)
            reach = _Reach(
                _HTTP, frozenset(methods) if methods else None, compiled
            )
        key = (id(route), prefix)
        chain = tree.chains.get(key)
        if (
            chain is not None
            and _declares_anonymous(route, contexts)
            and key not in tree.closed
        ):
            mounts, relative = chain
            reach = replace(
                reach,
                pattern=compile_route(f"{relative}{route.path}"),
                within=tree.within[key],
                mounts=tuple(compile_mount(mount) for mount in mounts),
            )
            public.append(reach)
            declared[key] = (reach, template)
        else:
            rivals.append((template, reach))
    if not public:
        return _Routes()
    by_depth: dict[int, list[_Reach]] = {}
    anywhere: list[_Reach] = []
    for template, reach in rivals:
        if _spans_depths(template):
            anywhere.append(reach)
        else:
            by_depth.setdefault(template.count("/"), []).append(reach)
    return _Routes(
        public=tuple(public),
        by_depth=MappingProxyType(
            {depth: tuple(reaches) for depth, reaches in by_depth.items()}
        ),
        anywhere=tuple(anywhere),
        declared=MappingProxyType(declared),
        redirects=_redirects_slashes(app),
        nodes=tuple(
            (
                node,
                _Reach(
                    _EITHER,
                    None,
                    compile_route(f"{under.rstrip('/')}/{{path:path}}"),
                ),
            )
            for node, under in tree.nodes
        ),
    )


def _redirects_slashes(app: Any) -> bool:  # noqa: ANN401
    """Return whether the app's router redirects a path to its other spelling.

    Without the redirect, a path that misses a route by its trailing slash
    goes to what the router answers when nothing matched, such as a frontend
    fallback or a default app.
    """
    routed = _route_source(app, unwrap_middleware=True)
    return bool(
        getattr(getattr(routed, "router", routed), "redirect_slashes", False)
    )


@dataclass(slots=True)
class _Tree:
    """Where each routing node of an app sits, and what each route sits in."""

    nodes: list[tuple[int, str]] = field(default_factory=list)
    within: dict[tuple[int, str], frozenset[int]] = field(default_factory=dict)
    closed: set[tuple[int, str]] = field(default_factory=set)
    chains: dict[tuple[int, str], tuple[tuple[str, ...], str]] = field(
        default_factory=dict
    )


def _read_tree(
    app: Any,  # noqa: ANN401
    tree: _Tree,
    *,
    prefix: str = "",
    within: frozenset[int] = frozenset(),
    mounts: tuple[str, ...] = (),
    relative: str = "",
    closed: bool = False,
    seen: frozenset[int] = frozenset(),
) -> None:
    """Record every mount and host, and where each route sits under them.

    A node answers the paths under it: a mount's own path, or for a host
    or a node of another kind, the path of the router holding it. A route
    is recorded with the mounts above it and the path it sits under within
    the innermost one, because Starlette matches each mount on its own. A
    route under a node that turns a request away to anything but a `404` is
    closed, because what answers instead is not the route.
    """
    routed = _route_source(app, unwrap_middleware=True)
    if routed is None or id(routed) in seen:
        return
    seen |= {id(routed)}
    for route in getattr(routed, "routes", None) or ():
        included = getattr(route, "original_router", None)
        if included is not None:
            added = getattr(
                getattr(route, "include_context", None), "prefix", ""
            )
            _read_tree(
                included,
                tree,
                prefix=f"{prefix}{added}",
                within=within,
                mounts=mounts,
                relative=f"{relative}{added}",
                closed=closed,
                seen=seen,
            )
            continue
        if _is_route(route):
            key = (id(route), prefix)
            tree.within[key] = within
            tree.chains[key] = (mounts, relative)
            if closed:
                tree.closed.add(key)
            continue
        mount = _is_mount(route)
        under = f"{prefix}{route.path}" if mount else prefix
        tree.nodes.append((id(route), under))
        _read_tree(
            getattr(route, "app", None),
            tree,
            prefix=under,
            within=within | {id(route)},
            mounts=(*mounts, route.path) if mount else mounts,
            relative="" if mount else relative,
            closed=closed or _turns_away_elsewhere(route, routed),
            seen=seen,
        )


def _turns_away_elsewhere(route: Any, holder: Any) -> bool:  # noqa: ANN401
    """Return whether a request this node does not take is answered by more than a `404`.

    A mount takes every path under it. Any other node, such as a host, may
    turn a request for its paths away, and its router then answers with its
    default.
    """
    if _is_mount(route):
        return False
    from starlette.routing import Router  # noqa: PLC0415

    default = getattr(getattr(holder, "router", holder), "default", None)
    return getattr(default, "__func__", None) is not Router.not_found


def _litestar_routes(app: Any) -> _Routes:  # noqa: ANN401
    """Read whether a Litestar app declares any handler public.

    Litestar's own router answers which handler serves a request, however
    its path is spelled and whatever a mount answers under it, so what is
    kept is the app to ask.
    """
    declared = any(
        handler.opt.get(ANONYMOUS_OPT)
        for _, route, _ in walk_routes(app)
        for handler in _litestar_handlers(route) or ()
    )
    return _Routes(litestar=app) if declared else _Routes()


def _litestar_serves_publicly(app: Any, scope: Scope) -> bool:  # noqa: ANN401
    """Return whether Litestar dispatches the request to a public handler.

    Asked of Litestar's own router, which picks the handler by path, by
    method, and for a websocket, so the handler it names is the one that
    runs. A request it would refuse is not served publicly.
    """
    from litestar.exceptions import HTTPException  # noqa: PLC0415
    from litestar.utils import normalize_path  # noqa: PLC0415

    root_path = scope.get("root_path", "")
    path = (
        scope["path"].split(root_path, maxsplit=1)[-1]
        if root_path
        else scope["path"]
    )
    try:
        _, handler, *_ = app.asgi_router.handle_routing(
            path=normalize_path(path), method=scope.get("method")
        )
    except (HTTPException, KeyError):
        # `KeyError` for a websocket asking a path only HTTP handlers answer.
        return False
    return bool(handler.opt.get(ANONYMOUS_OPT))


_STARLETTE_PARAMETER = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([a-zA-Z_][a-zA-Z0-9_]*))?\}"
)
"""A path parameter in a Starlette route's path, with its optional converter."""

_SAMPLES: Final = {
    "str": "grelmicro-probe",
    "path": "grelmicro/probe",
    "int": "40961257",
    "float": "4096.1257",
    "uuid": "00000000-0000-4000-8000-00000c0ffee0",
}
"""A value for each converter that no literal route is likely declared with."""

_FALLBACK_SAMPLES: Final = (
    "4096-12-31",
    "4096-12-31T23:59:59",
    "grelmicro",
    "4096",
    "g",
)
"""Values tried for a converter the app registered itself."""


def _spans_depths(template: str) -> bool:
    """Return whether a URL this template matches may have more segments.

    Starlette's own `str`, `int`, `float` and `uuid` converters each match
    within one segment. Any other, `path` included, may match a slash, and so
    may one registered under one of those names to replace it. A route using
    one is held against URLs of every depth.
    """
    from starlette.convertors import (  # noqa: PLC0415  # codespell:ignore
        CONVERTOR_TYPES,
        FloatConvertor,
        IntegerConvertor,
        StringConvertor,
        UUIDConvertor,
    )

    single = (StringConvertor, IntegerConvertor, FloatConvertor, UUIDConvertor)
    return any(
        type(CONVERTOR_TYPES.get(match.group(2) or "str")) not in single
        for match in _STARLETTE_PARAMETER.finditer(template)
    )


def _sample_url(template: str) -> str | None:
    """Return a URL of this route whose parameters avoid literal paths.

    Each value is one the parameter's converter accepts. `None` when no
    sample fits a converter the app registered itself.
    """
    from starlette.convertors import (  # noqa: PLC0415  # codespell:ignore
        CONVERTOR_TYPES,
    )

    pieces: list[str] = []
    last = 0
    for match in _STARLETTE_PARAMETER.finditer(template):
        name = match.group(2) or "str"
        regex = CONVERTOR_TYPES[name].regex
        candidates = (
            _SAMPLES.get(name),
            *_SAMPLES.values(),
            *_FALLBACK_SAMPLES,
        )
        value = next(
            (
                candidate
                for candidate in candidates
                if candidate is not None and re.fullmatch(regex, candidate)
            ),
            None,
        )
        if value is None:
            return None
        pieces.extend((template[last : match.start()], value))
        last = match.end()
    pieces.append(template[last:])
    return "".join(pieces)


def _litestar_handlers(route: Any) -> list[Any] | None:  # noqa: ANN401
    """Return a Litestar route's handlers, or `None` for another framework's.

    An HTTP route holds one handler per method, and a websocket route holds
    a single one.
    """
    handlers = getattr(route, "route_handlers", None)
    if handlers is not None:
        return list(handlers)
    handler = getattr(route, "route_handler", None)
    return None if handler is None else [handler]


def _declares_anonymous(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...] = (),
) -> bool:
    """Return whether a route declared `Anonymous()`.

    On the route, on the router that holds it, or where that router was
    included.
    """
    declared = getattr(route, "dependant", None)  # codespell:ignore
    if any(
        is_anonymous_declaration(dependency.call)
        for dependency in getattr(declared, "dependencies", ())
    ):
        return True
    return any(
        is_anonymous_declaration(getattr(dependency, "dependency", None))
        for context in contexts
        for dependency in getattr(context, "dependencies", ()) or ()
    )


AUTHENTICATED_MARKER = "__grelmicro_authenticated__"
"""Set on what a route declares `Authenticated` with, so a reader finds it.

Read by attribute rather than by identity, so a declaration made before its
module was imported again is still recognised as one. The FastAPI
dependency carries `True`, and its scopes come from the dependency tree. A
Litestar guard carries the scopes it requires.
"""


def is_anonymous_declaration(call: object) -> bool:
    """Return whether a dependency is `Anonymous()`, which computes nothing.

    Resolving it first gates nothing, so a route declaring it stays as
    cacheable and as replayable as one declaring no dependency at all.
    """
    return bool(getattr(call, _ANONYMOUS_MARKER, False))


def _litestar_declares_public(
    route: Any,  # noqa: ANN401
    method: str,
) -> bool:
    """Return whether a Litestar route declares this method public.

    Litestar's router always runs the handler that declared it for the URLs
    it routes there, so the declaration is the answer.
    """
    return any(
        handler.opt.get(ANONYMOUS_OPT)
        and method in getattr(handler, "http_methods", ())
        for handler in _litestar_handlers(route) or ()
    )


def route_scopes(
    route: Any,  # noqa: ANN401
    method: str,
    contexts: tuple[Any, ...] = (),
) -> tuple[str, ...]:
    """Return every scope an `Authenticated` on this route requires, in order.

    A FastAPI route declares them through its dependency tree, a router's
    included. A Litestar handler declares them as guards, a router's and
    the app's included. A Starlette endpoint declares them with the
    `Authenticated` decorator.
    """
    return tuple(
        dict.fromkeys(
            scope
            for declared in _declarations(route, method, contexts)
            for scope in declared
        )
    )


def requires_caller(
    route: Any,  # noqa: ANN401
    method: str,
    contexts: tuple[Any, ...] = (),
) -> bool:
    """Return whether the route refuses a request that sends no credential.

    True when it declares `Authenticated`, or on FastAPI reads the caller
    through `CurrentPrincipal` or `Claims`, whatever scopes it names.
    """
    return bool(_declarations(route, method, contexts))


def _declarations(
    route: Any,  # noqa: ANN401
    method: str,
    contexts: tuple[Any, ...],
) -> list[tuple[str, ...]]:
    """Return the scopes of each declaration on the route requiring a caller."""
    handlers = _litestar_handlers(route)
    if handlers is not None:
        return [
            tuple(getattr(guard, AUTHENTICATED_MARKER))
            for handler in handlers
            if _handles(handler, method)
            for guard in handler.resolve_guards()
            if hasattr(guard, AUTHENTICATED_MARKER)
        ]
    found: list[tuple[str, ...]] = []
    endpoint = getattr(route, "endpoint", None)
    # An endpoint class answers each method from a method of its own.
    target = (
        getattr(endpoint, method.lower(), None)
        if isinstance(endpoint, type)
        else endpoint
    )
    if hasattr(target, AUTHENTICATED_MARKER):
        found.append(tuple(getattr(target, AUTHENTICATED_MARKER)))
    declared = getattr(route, "dependant", None)  # codespell:ignore
    pending = [
        *getattr(declared, "dependencies", ()),
        *_included_dependency_trees(route, contexts),
    ]
    while pending:
        dependency = pending.pop(0)
        if getattr(dependency.call, AUTHENTICATED_MARKER, False):
            # What `SecurityScopes` hands it: the scopes of every `Security`
            # around it as well as its own, under either spelling FastAPI
            # has used for them.
            found.append(
                (
                    *(getattr(dependency, "parent_oauth_scopes", None) or ()),
                    *(
                        getattr(dependency, "own_oauth_scopes", None)
                        or getattr(dependency, "security_scopes", None)
                        or ()
                    ),
                )
            )
        pending.extend(dependency.dependencies)
    return found


def _handles(handler: Any, method: str) -> bool:  # noqa: ANN401
    """Return whether a Litestar handler answers `method`.

    A websocket handler names no method, so it answers whichever is asked.
    """
    methods = getattr(handler, "http_methods", None)
    return methods is None or method in methods


_ENDPOINT_METHODS: Final = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
"""Methods an endpoint class may answer, which its route does not list."""


def refuse_unreachable_routes(
    app: Any,  # noqa: ANN401
    exclude: tuple[str, ...],
) -> None:
    """Refuse a route that requires a caller where none is ever required.

    A route in `exclude` never has a token read, so one requiring a caller
    answers `401` to every request. A route declaring `Anonymous()` and
    requiring a caller refuses the requests `Anonymous()` is there to serve.

    Raises:
        TypeError: Naming the method and the path of the first such route.
    """
    for prefix, route, contexts in walk_routes(app, unwrap_middleware=True):
        template = f"{prefix}{getattr(route, 'path_format', route.path)}"
        excluded = not selects(template, include=(), exclude=exclude)
        # Sorted, so the method a refusal names is the same on every run.
        for method in sorted(
            getattr(route, "methods", None) or _ENDPOINT_METHODS
        ):
            if not requires_caller(route, method, contexts):
                continue
            if excluded:
                where = "is in exclude, so a token is never read there,"
            elif _declares_public(route, method, contexts):
                where = "declares Anonymous()"
            else:
                continue
            msg = (
                f"{method} {template} {where} and requires a caller through "
                f"Authenticated, CurrentPrincipal or Claims, so it refuses "
                f"every request it was written to serve. Read "
                f"OptionalPrincipal on a public route, or take away what "
                f"makes the route public."
            )
            raise TypeError(msg)


def _declares_public(
    route: Any,  # noqa: ANN401
    method: str,
    contexts: tuple[Any, ...],
) -> bool:
    """Return whether the route declares itself public, on either framework."""
    if _litestar_handlers(route) is not None:
        return _litestar_declares_public(route, method)
    return _declares_anonymous(route, contexts)


def _included_dependency_trees(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...],
) -> list[Any]:
    """Return the dependency trees a route's routers were included with.

    FastAPI keeps what `include_router(dependencies=...)` names on the
    include context rather than in the route's own tree, and resolves it
    only when it serves the route, so the trees are built here the same way.
    """
    declared = [
        dependency
        for context in contexts
        for dependency in getattr(context, "dependencies", ()) or ()
        if callable(getattr(dependency, "dependency", None))
    ]
    if not declared:
        return []
    from fastapi.dependencies.utils import (  # noqa: PLC0415
        get_parameterless_sub_dependant,  # codespell:ignore
    )

    return [
        get_parameterless_sub_dependant(
            depends=dependency, path=route.path
        )  # codespell:ignore
        for dependency in declared
    ]


SECURITY_SCHEME: Final = "AuthenticatedRequests"
"""Name the schema publishes the bearer token scheme under."""

_OPENID_CONFIGURATION: Final = "/.well-known/openid-configuration"
"""Where OpenID Connect discovery appends its metadata to an issuer."""

_OPERATION_METHODS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
"""Every method an OpenAPI path item can carry an operation under."""

_CHALLENGE_HEADER: Final = {
    "schema": {"type": "string"},
    "description": "The bearer token challenge, as RFC 6750 defines it.",
}
"""The `WWW-Authenticate` header a refusal carries."""

_TOO_MANY_REQUESTS: Final = "429"
"""Status a banned caller is answered with."""


def operation_authentication(
    app: Any,  # noqa: ANN401
    *,
    anonymous: bool,
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], list[str]]]:
    """Return the public operations, and the scopes each covered one needs.

    Keyed by the path the schema publishes and the lowercased method, so
    both read straight against the schema's paths. An operation is public
    only when the middleware serves it without a credential, which a
    declaration alone does not settle: another route may answer its URL.
    With `anonymous` false no declaration counts, as for a middleware added
    by hand.
    """
    served = routes_of(app)
    public: set[tuple[str, str]] = set()
    scopes: dict[tuple[str, str], list[str]] = {}
    for prefix, route, contexts in walk_routes(app, unwrap_middleware=True):
        path = f"{prefix}{getattr(route, 'path_format', route.path)}"
        # `None` for an endpoint class, which answers whatever it defines.
        for method in getattr(route, "methods", None) or ():
            key = (path, method.lower())
            if anonymous and served.serves_publicly(route, method, prefix):
                public.add(key)
            else:
                scopes[key] = list(route_scopes(route, method, contexts))
    return public, scopes


def document_operations(
    schema: dict[str, Any],
    *,
    verifier: object,
    bans: bool,
    exclude: tuple[str, ...],
    public: set[tuple[str, str]],
    scopes: dict[tuple[str, str], list[str]],
    media_type: str,
    model: type[BaseModel],
) -> None:
    """Require the scheme on every covered operation, with its refusals.

    Shared by every framework that builds a schema, so the same app is
    described the same way whichever one serves it.
    """
    ref = add_error_schema(schema, model)
    content = {media_type: {"schema": {"$ref": ref}}} if ref else {}
    schema.setdefault("components", {}).setdefault(
        "securitySchemes", {}
    ).setdefault(SECURITY_SCHEME, security_scheme(verifier))
    inherited = schema.get("security")
    for path, item in (schema.get("paths") or {}).items():
        for method, operation in item.items():
            if method not in _OPERATION_METHODS or not selects(
                path, include=(), exclude=exclude
            ):
                continue
            if (path, method) in public:
                _offer_scheme(
                    operation, content, bans=bans, inherited=inherited
                )
                continue
            _require_scheme(
                operation,
                scopes.get((path, method), []),
                content,
                bans=bans,
                inherited=inherited,
            )


def _require_scheme(
    operation: dict[str, Any],
    required: list[str],
    content: dict[str, Any],
    *,
    bans: bool,
    inherited: list[dict[str, list[str]]] | None,
) -> None:
    """Require the scheme on one operation, and describe what it answers.

    An operation naming no requirement of its own inherits the schema's, so
    that one is written onto it before ours is joined, rather than replaced
    by ours.
    """
    # OpenAPI lists alternatives, each naming what is required together.
    # The middleware requires the bearer token whichever alternative the
    # route checks itself, so it joins every one of them rather than
    # standing beside them as a way around them.
    security = operation.get("security")
    if security is None and inherited:
        security = operation["security"] = [
            dict(alternative) for alternative in inherited
        ]
    if security:
        for alternative in security:
            alternative.setdefault(SECURITY_SCHEME, required)
    else:
        operation["security"] = [{SECURITY_SCHEME: required}]
    _describe_refusals(operation, required, content, bans=bans)


def _offer_scheme(
    operation: dict[str, Any],
    content: dict[str, Any],
    *,
    bans: bool,
    inherited: list[dict[str, list[str]]] | None,
) -> None:
    """Offer the scheme on a public operation, where a caller may leave it out.

    Each alternative the operation already names is kept, and joined by a
    copy that adds the bearer token, so a client sees the token as optional
    and whatever else the route checks as still required.
    """
    security = operation.get("security")
    if security is None and inherited:
        security = [dict(alternative) for alternative in inherited]
    if not security:
        security = [{}]
    if not any(SECURITY_SCHEME in alternative for alternative in security):
        security = [
            *security,
            *({**alternative, SECURITY_SCHEME: []} for alternative in security),
        ]
    operation["security"] = security
    _describe_refusals(operation, [], content, bans=bans)


def _describe_refusals(
    operation: dict[str, Any],
    required: list[str],
    content: dict[str, Any],
    *,
    bans: bool,
) -> None:
    """Describe the refusals an operation covered by the scheme can answer."""
    responses = operation.setdefault("responses", {})
    responses.setdefault(
        "401",
        {
            "description": (
                "The request carried no valid bearer token. "
                "`WWW-Authenticate` says how to authenticate."
            ),
            "headers": {"WWW-Authenticate": _CHALLENGE_HEADER},
            "content": content,
        },
    )
    if required:
        responses.setdefault(
            "403",
            {
                "description": (
                    "The token does not grant every scope this operation "
                    "needs. `WWW-Authenticate` names them."
                ),
                "headers": {"WWW-Authenticate": _CHALLENGE_HEADER},
                "content": content,
            },
        )
    if bans:
        responses.setdefault(
            _TOO_MANY_REQUESTS,
            {
                "description": (
                    "The caller is banned for presenting forged tokens. "
                    "Retry after the delay in `Retry-After`."
                ),
                "headers": {
                    "Retry-After": {
                        "schema": {"type": "integer"},
                        "description": "Seconds to wait before retrying.",
                    }
                },
                "content": content,
            },
        )


def security_scheme(verifier: object) -> dict[str, Any]:
    """Return the security scheme a verifier's tokens are described by.

    `openIdConnect` points a client at the issuer's discovery document, so
    it is published only when that is the document the verifier found, or
    before discovery has run. A provider publishing RFC 8414 metadata alone
    is described as a bearer token, which every client can send.
    """
    config = getattr(verifier, "config", None)
    if isinstance(config, DiscoveryConfig):
        url = f"{config.issuer[0].rstrip('/')}{_OPENID_CONFIGURATION}"
        found = getattr(verifier, "metadata_url", None)
        if found is None or found == url:
            return {"type": "openIdConnect", "openIdConnectUrl": url}
    return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


class AuthenticatedRequestsConfig(BaseModel, frozen=True, extra="forbid"):
    """Authenticated Requests Config.

    Where authentication does not apply. Every other path is authenticated,
    and there is no `include`: a mistyped one would leave an endpoint public
    without a word. Nothing here is read from the environment, because every
    field decides what the service is protected by.
    """

    exclude: Annotated[
        PathPatterns,
        Doc(
            "Paths served without a credential, such as health probes. "
            "Exact match unless the pattern ends with `*`, which matches as "
            "a prefix."
        ),
    ] = ()


class AuthenticatedRequestsMiddleware:
    """Authenticate every request before the app sees it.

    Reads the bearer token from `Authorization`, verifies it, and puts the
    verified caller in `scope["user"]` and `scope["auth"]`, which is where
    `request.user`, `request.auth` and Starlette's `@requires` look.

    ```python
    from grelmicro.http import AuthenticatedRequestsMiddleware
    from grelmicro.security import JWTVerifier

    app.add_middleware(
        AuthenticatedRequestsMiddleware,
        verifier=JWTVerifier.discover(
            "https://auth.example.com/", audience="orders-api"
        ),
        exclude=("/livez", "/readyz"),
    )
    ```

    Register `AuthenticatedRequests(...)` instead to have
    `micro.install(app)` add it, place it and open its verifier for you.

    What a refused caller is told:

    - No credential, or one in another scheme such as `Basic`: `401`.
    - A token that does not verify: `401`, with the reason.
    - More than one credential: `400`.
    - A caller `bans` refuses: `429`, with `Retry-After`.
    - Keys that have not loaded: `503`.

    Each is rendered by the app's `ErrorResponses`, with the
    `WWW-Authenticate` challenge RFC 6750 gives it.

    A token naming a key the verifier does not hold waits for one refresh
    and is verified again, so the first request after a rotation is served.
    The verifier fetches at most once per `retry_interval`, so a caller
    inventing key ids costs one fetch, not one per request.

    The middleware is pure ASGI. It acts on `http` and `websocket` scopes and
    passes every other scope through untouched. A refused websocket is
    answered with the same `401` on a server that supports the denial
    response extension, and closed before the handshake completes on one
    that does not.
    """

    def __init__(
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        verifier: Annotated[
            TokenVerifier,
            Doc("Verifies each bearer token, such as a `JWTVerifier`."),
        ],
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths served without a credential. Exact match unless the "
                "pattern ends with `*`, which matches as a prefix."
            ),
        ] = (),
        bans: Annotated[
            ClientBans | None,
            Doc(
                "Refuses a caller that keeps presenting forged tokens, "
                "before its next token is verified. Counts only the "
                "reasons it is configured with."
            ),
        ] = None,
        trusted: Annotated[
            TrustedProxies | None,
            Doc(
                "The proxies whose forwarded entries may be believed, for "
                "resolving the caller a ban is counted against. Required "
                "with `bans`."
            ),
        ] = None,
        public: Annotated[
            _PublicRoutes | None,
            Doc(
                "The routes that declared `Anonymous()`, filled by "
                "`micro.install(app)`. A middleware added by hand serves "
                "public paths through `exclude` instead."
            ),
        ] = None,
    ) -> None:
        """Initialize the middleware with the verifier it trusts.

        Raises:
            TypeError: If `exclude` is a single string, or `bans` is given
                without `trusted`.
            ValueError: If a pattern in `exclude` matches every path.
        """
        if bans is not None and trusted is None:
            msg = (
                "AuthenticatedRequestsMiddleware needs trusted= to resolve "
                "the caller a ban is counted against. Without it the only "
                "address left is the socket peer, which behind an ingress "
                "is the ingress, and one forged token would ban everyone."
            )
            raise TypeError(msg)
        self.app = app
        self._verifier = verifier
        self._exclude = _narrower_than_everything(
            as_patterns(exclude, name="exclude")
        )
        self._bans = bans
        self._trusted = trusted
        self._public = public

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Authenticate the request, then serve or refuse it."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self._serves_anonymously(scope):
            # Set only when nothing else did, so an authentication the app
            # runs itself outside this one keeps its caller.
            scope.setdefault("user", _ANONYMOUS)
            scope.setdefault("auth", _ANONYMOUS)
            await self.app(scope, receive, send)
            return
        try:
            caller = await self._authenticate(scope)
        except _REFUSALS as error:
            await _refuse(scope, receive, send, error)
            return
        scope["user"] = caller
        scope["auth"] = caller
        await self.app(scope, receive, send)

    def _serves_anonymously(self, scope: Scope) -> bool:
        """Return whether the request is served without verifying a credential.

        An excluded path never reads one. A public route reads one only when
        a bearer token is presented, so a request sending none is served
        anonymously and one sending a token is verified like any other.
        """
        if self._exclude and not selects(
            route_path(scope), include=(), exclude=self._exclude
        ):
            return True
        public = self._public
        return (
            public is not None
            and not _presents_bearer(scope)
            and public.matches(scope)
        )

    async def _authenticate(self, scope: Scope) -> JWTClaims:
        """Return the verified caller, or raise what the caller is told."""
        token = _bearer_token(scope)
        client = self._client_of(scope)
        bans = self._bans
        if client is not None and bans is not None and bans.banned(client):
            raise ClientBannedError(retry_after=bans.banned_for(client))
        try:
            return await self._verified(token)
        except TokenRejectedError as error:
            if client is not None and bans is not None:
                bans.record(client, error.reason)
            raise

    async def _verified(self, token: str) -> JWTClaims:
        """Verify `token`, waiting for one refresh when it names a new key."""
        verifier = self._verifier
        try:
            return verifier.verify(token)
        except TokenRejectedError as error:
            if error.reason is not TokenRejectedReason.UNKNOWN_KEY:
                raise
            if not await self._refreshed():
                raise
        return verifier.verify(token)

    async def _refreshed(self) -> bool:
        """Return whether a refresh loaded keys the verifier did not hold.

        A refresh that fails keeps the keys already loaded, so the token is
        refused for the key it named rather than the service answering
        `503` for a key nobody can vouch for.
        """
        refresh = getattr(self._verifier, "refresh", None)
        if refresh is None:
            return False
        try:
            return bool(await refresh())
        except SigningKeysUnavailableError:
            return False

    def _client_of(self, scope: Scope) -> str | None:
        """Return the address a ban is counted against, when bans are on."""
        if self._bans is None:
            return None
        return bucket_of(scope, key=None, trusted=self._trusted).key


def _narrower_than_everything(exclude: tuple[str, ...]) -> tuple[str, ...]:
    """Return `exclude`, refusing a pattern that matches every path.

    Raises:
        ValueError: If a pattern is `*` or `/*`, which would serve every
            request without a credential.
    """
    for pattern in exclude:
        if pattern in _EVERY_PATH:
            msg = (
                f"exclude={exclude!r} matches every path, so no request would "
                f"be authenticated. Leave AuthenticatedRequests unregistered "
                f"to serve the app without a credential."
            )
            raise ValueError(msg)
    return exclude


_EVERY_PATH: Final = frozenset({"*", "/*"})
"""The patterns that match every path an app serves."""


def _bearer_token(scope: Scope) -> str:
    """Return the bearer token the request carries.

    Raises:
        AmbiguousCredentialsError: If it carries more than one credential.
        AuthenticationRequiredError: If it carries none, one in another
            scheme, or the bearer scheme with no token behind it.
    """
    credentials = _credentials(scope)
    if len(credentials) > 1:
        raise AmbiguousCredentialsError
    token = _bearer_of(credentials[0]) if credentials else ""
    if not token:
        raise AuthenticationRequiredError
    return token


def _presents_bearer(scope: Scope) -> bool:
    """Return whether the request presents a bearer token, or several credentials.

    Several credentials count, so a public route refuses them as ambiguous
    rather than choosing one to ignore.
    """
    credentials = _credentials(scope)
    return len(credentials) > 1 or any(
        _bearer_of(credential) for credential in credentials
    )


def _credentials(scope: Scope) -> list[bytes]:
    """Return every `Authorization` header the request carries."""
    return [
        value for name, value in scope["headers"] if name == b"authorization"
    ]


def _bearer_of(credential: bytes) -> str:
    """Return the bearer token in a credential, or an empty string for none.

    RFC 7235 allows one or more spaces between the scheme and the token.
    """
    scheme, _, rest = credential.decode("latin-1").partition(" ")
    token = rest.lstrip(" ")
    return token if scheme.lower() == _BEARER else ""


async def _refuse(
    scope: Scope, receive: Receive, send: Send, error: Exception
) -> None:
    """Answer a refusal in the format the app answers every refusal with."""
    app = scope.get("app")
    registered = getattr(
        getattr(app, "state", None), "grelmicro_error_responses", None
    )
    errors = registered if registered is not None else ErrorResponses()
    rendered = errors.render(error, instance=scope.get("path"))
    if rendered is None:  # pragma: no cover - every refusal has a kind
        raise error
    if scope["type"] == "http":
        await send_error(send, rendered)
        return
    await _deny_websocket(scope, receive, send, rendered)


async def _deny_websocket(
    scope: Scope, receive: Receive, send: Send, rendered: RenderedError
) -> None:
    """Refuse a websocket handshake, with the refusal's status when possible.

    The denial response extension carries a whole HTTP response, the `401`
    and its challenge included. A server without it can only close the
    handshake, which it answers `403` with no headers. Accepting the
    connection to close it with a code would complete the handshake for a
    caller that never authenticated, so that is never done.
    """
    message = await receive()
    if message["type"] != "websocket.connect":
        return
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


class AuthenticatedRequests:
    """Authenticate every request, wired by `micro.install(app)`.

    Register it and `install` adds `AuthenticatedRequestsMiddleware`, ahead
    of every other middleware of ours that can answer a request:

    ```python
    from grelmicro import Grelmicro
    from grelmicro.http import AuthenticatedRequests, ErrorResponses
    from grelmicro.security import JWTVerifier

    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(
                JWTVerifier.discover(
                    "https://auth.example.com/", audience="orders-api"
                ),
                exclude=("/livez", "/readyz"),
            ),
        ]
    )
    ```

    Every request is authenticated except the paths in `exclude`. A route
    declaring `Anonymous()` authenticates a request only when it sends a
    token. The verifier is opened with the app,
    so its keys load before the first request and stay fresh while it
    serves.

    Nothing is read from the environment and nothing is live: every setting
    decides what the service is protected by, so each one changes with a
    deploy.

    Read more in the [Authentication](../http/authentication.md) docs.
    """

    kind: ClassVar[str] = "authenticated_requests"
    singleton: ClassVar[bool] = True
    singleton_reason: ClassVar[str] = (
        "One authentication answers for the whole app, because a second "
        "would require every token to pass both. To accept tokens from "
        "several issuers, pass one verifier that accepts them"
    )
    asgi_authenticates: ClassVar[bool] = True
    """Placed ahead of every other answering middleware of ours at install."""

    def __init__(
        self,
        verifier: Annotated[
            TokenVerifier,
            Doc("Verifies each bearer token, such as a `JWTVerifier`."),
        ],
        *,
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths served without a credential, such as health probes. "
                "Same matching as every other middleware."
            ),
        ] = (),
        bans: Annotated[
            ClientBans | None,
            Doc("Refuses a caller that keeps presenting forged tokens."),
        ] = None,
        trusted: Annotated[
            TrustedProxies | None,
            Doc(
                "The proxies whose forwarded entries may be believed, for "
                "resolving the caller a ban is counted against."
            ),
        ] = None,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
        openapi: Annotated[
            bool,
            Doc(
                "Describe the security scheme, and the `401` and `403` a "
                "covered operation answers, in the OpenAPI schema. Only "
                "FastAPI builds one."
            ),
        ] = True,
    ) -> None:
        """Authenticate every request through the registered middleware.

        Raises:
            TypeError: If `exclude` is a single string, or `bans` is given
                without `trusted`.
        """
        config = build_config(
            AuthenticatedRequestsConfig,
            exclude=as_patterns(exclude, name="exclude"),
        )
        self._setup(
            config,
            verifier=verifier,
            bans=bans,
            trusted=trusted,
            name=name,
            openapi=openapi,
        )

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            AuthenticatedRequestsConfig,
            Doc("The pre-built authenticated requests configuration."),
        ],
        verifier: Annotated[
            TokenVerifier,
            Doc("Verifies each bearer token, such as a `JWTVerifier`."),
        ],
        *,
        bans: Annotated[
            ClientBans | None,
            Doc("Refuses a caller that keeps presenting forged tokens."),
        ] = None,
        trusted: Annotated[
            TrustedProxies | None,
            Doc("The proxies whose forwarded entries may be believed."),
        ] = None,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
        openapi: Annotated[
            bool,
            Doc(
                "Describe the security scheme, and the `401` and `403` a "
                "covered operation answers, in the OpenAPI schema. Only "
                "FastAPI builds one."
            ),
        ] = True,
    ) -> Self:
        """Build the component from a configuration that is already whole.

        The one declarative door. The verifier stays beside the config,
        because it is an object holding keys rather than a setting.
        """
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            config,
            verifier=verifier,
            bans=bans,
            trusted=trusted,
            name=name,
            openapi=openapi,
        )
        return instance

    def _setup(
        self,
        config: AuthenticatedRequestsConfig,
        *,
        verifier: TokenVerifier,
        bans: ClientBans | None,
        trusted: TrustedProxies | None,
        name: str,
        openapi: bool,
    ) -> None:
        """Hold the configuration and the objects the middleware reads."""
        self._config = config
        self._verifier = verifier
        self._bans = bans
        self._trusted = trusted
        self._name = name
        self._openapi = openapi
        self._stack: AsyncExitStack | None = None
        self._public = _PublicRoutes()
        # Built once here so a mistake is refused where it is written,
        # rather than on the first request the app serves.
        AuthenticatedRequestsMiddleware(_nothing, **self._options())

    def _options(self) -> dict[str, Any]:
        """Return the arguments the middleware is built with."""
        return {
            "verifier": self._verifier,
            "exclude": self._config.exclude,
            "bans": self._bans,
            "trusted": self._trusted,
            "public": self._public,
        }

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def config(self) -> AuthenticatedRequestsConfig:
        """Return the configuration the middleware reads."""
        return self._config

    @property
    def verifier(self) -> TokenVerifier:
        """Return the verifier every token is checked against."""
        return self._verifier

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with."""
        return AuthenticatedRequestsMiddleware, self._options()

    def document_openapi(
        self,
        app: Annotated[Any, Doc("The FastAPI application to describe.")],  # noqa: ANN401
    ) -> None:
        """Describe the security scheme and its refusals in the schema.

        Called by the FastAPI and Litestar integrations after the middleware
        is added. A framework that builds no schema never calls it.
        """
        if not self._openapi:
            return
        if getattr(app, "asgi_router", None) is not None:
            from grelmicro.integrations.litestar import (  # noqa: PLC0415
                _document_authentication,
            )

            _document_authentication(app, self._options())
            return
        from grelmicro.integrations.fastapi import (  # noqa: PLC0415
            document_authenticated_requests,
        )

        document_authenticated_requests(app)

    def read_routes(
        self,
        app: Annotated[Any, Doc("The application to read the routes off.")],  # noqa: ANN401
    ) -> None:
        """Read `Anonymous()` off every route the app declares.

        Called by the integration after the middleware is added. The app is
        read again when it starts, so a route added between the two counts
        as well.

        Raises:
            TypeError: If a route requiring a caller declares `Anonymous()`
                or sits in `exclude`, where it could never get one.
        """
        refuse_unreachable_routes(app, self._config.exclude)
        self._public.read(app)

    def handled_exceptions(self) -> tuple[type[Exception], ...]:
        """Return what this component answers rather than letting through.

        The middleware answers what it refuses itself. These are what a
        route raises, such as a missing scope, and registering the
        component is the opt-in for answering those the same way.
        """
        return (*_REFUSALS, InsufficientScopeError)

    async def __aenter__(self) -> Self:
        """Read the routes again, and open the verifier so its keys load.

        Raises:
            TypeError: If a route added since install requires a caller where
                it could never get one.
        """
        for app in self._public.apps:
            refuse_unreachable_routes(app, self._config.exclude)
        self._public.reread()
        stack = AsyncExitStack()
        enter = getattr(self._verifier, "__aenter__", None)
        if enter is not None:
            await stack.enter_async_context(
                cast("AbstractAsyncContextManager[Any]", self._verifier)
            )
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the verifier, stopping its background refresh."""
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()
        return None


async def _nothing(scope: Scope, receive: Receive, send: Send) -> None:
    """Stand in for the app, so the options can be checked without one."""
