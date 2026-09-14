"""Authentication at the HTTP edge."""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self, cast

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._config import build_config
from grelmicro._paths import (
    PathPatterns,
    as_patterns,
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
from grelmicro.http._ratelimit import bucket_of
from grelmicro.security.bans import ClientBannedError
from grelmicro.security.jwks import SigningKeysUnavailableError
from grelmicro.security.jwt import TokenRejectedError, TokenRejectedReason

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

ANONYMOUS_OPT = "grelmicro_anonymous"
"""The `opt` key a Litestar handler declares itself public under."""

_LITESTAR_PARAMETER = re.compile(r"\{([^}]+)\}")
"""A path parameter in a Litestar route's `path_format`."""


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


class _PublicRoutes:
    """The routes that declared `Anonymous()`, read off the app.

    Read when `micro.install(app)` adds the middleware, and again when the
    app starts, so a route declared between the two counts as well.
    """

    def __init__(self) -> None:
        """Start with no app and no public route."""
        self._app: Any = None
        self._routes: tuple[
            tuple[Pattern[str], frozenset[str] | None], ...
        ] = ()

    def read(self, app: Any) -> None:  # noqa: ANN401
        """Read every route `app` declares public."""
        self._app = app
        self._routes = _public_routes(app)

    def reread(self) -> None:
        """Read the app again, for the routes declared since install."""
        if self._app is not None:
            self._routes = _public_routes(self._app)

    def matches(self, scope: Scope) -> bool:
        """Return whether the request asks for a route declared public.

        Per method, so a path that serves a public read and an
        authenticated write keeps the write authenticated.
        """
        routes = self._routes
        if not routes:
            return False
        path = route_path(scope)
        method = scope.get("method")
        return any(
            compiled.fullmatch(path) is not None
            and (methods is None or method in methods)
            for compiled, methods in routes
        )


def _public_routes(
    app: Any,  # noqa: ANN401
) -> tuple[tuple[Pattern[str], frozenset[str] | None], ...]:
    """Return the path and methods of every route that declared `Anonymous()`.

    Read off the resolved dependency tree, so a declaration a router makes
    for everything it holds counts for each of its routes. Each path is
    compiled with the framework's own compiler, so it matches exactly what
    the router matches.
    """
    found: list[tuple[Pattern[str], frozenset[str] | None]] = []
    for prefix, route, _ in walk_routes(app):
        handlers = getattr(route, "route_handlers", None)
        if handlers is not None:
            found.extend(_public_litestar_route(route, handlers))
            continue
        if not _declares_anonymous(route):
            continue
        from starlette.routing import compile_path  # noqa: PLC0415

        compiled, _, _ = compile_path(f"{prefix}{route.path}")
        methods = getattr(route, "methods", None)
        found.append((compiled, frozenset(methods) if methods else None))
    return tuple(found)


def _public_litestar_route(
    route: Any,  # noqa: ANN401
    handlers: Any,  # noqa: ANN401
) -> list[tuple[Pattern[str], frozenset[str] | None]]:
    """Return one Litestar route's public handlers, by path and method.

    A Litestar handler declares itself public through its `opt`. Its path
    parameters carry types Starlette's compiler does not read, so the path
    is compiled here: a `path` parameter spans slashes, and every other one
    spans a single segment.
    """
    methods = frozenset(
        method
        for handler in handlers
        if handler.opt.get(ANONYMOUS_OPT)
        for method in handler.http_methods
    )
    if not methods:
        return []
    return [(_litestar_pattern(route), methods)]


def _litestar_pattern(route: Any) -> Pattern[str]:  # noqa: ANN401
    """Compile a Litestar route's path into the pattern it matches."""
    template = route.path_format
    parameters = route.path_parameters
    pieces: list[str] = []
    last = 0
    for match in _LITESTAR_PARAMETER.finditer(template):
        pieces.append(re.escape(template[last : match.start()]))
        definition = parameters.get(match.group(1))
        spans = definition is not None and definition.full.endswith(":path")
        pieces.append(".*" if spans else "[^/]+")
        last = match.end()
    pieces.append(re.escape(template[last:]))
    return re.compile("".join(pieces))


def _declares_anonymous(route: Any) -> bool:  # noqa: ANN401
    """Return whether a route declared `Anonymous()`, itself or through a router."""
    declared = getattr(route, "dependant", None)  # codespell:ignore
    return any(
        getattr(dependency.call, _ANONYMOUS_MARKER, False)
        for dependency in getattr(declared, "dependencies", ())
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


def route_is_public(
    route: Any,  # noqa: ANN401
    method: str,
) -> bool:
    """Return whether a route serves this method without a credential."""
    handlers = getattr(route, "route_handlers", None)
    if handlers is not None:
        return any(
            handler.opt.get(ANONYMOUS_OPT) and method in handler.http_methods
            for handler in handlers
        )
    return _declares_anonymous(route)


def route_scopes(
    route: Any,  # noqa: ANN401
    method: str,
) -> tuple[str, ...]:
    """Return every scope an `Authenticated` on this route requires, in order.

    A FastAPI route declares them through its dependency tree, a router's
    included. A Litestar handler declares them as guards, a router's and
    the app's included.
    """
    found: list[str] = []
    handlers = getattr(route, "route_handlers", None)
    if handlers is not None:
        for handler in handlers:
            if method in handler.http_methods:
                for guard in handler.resolve_guards():
                    found.extend(getattr(guard, AUTHENTICATED_MARKER, ()))
        return tuple(dict.fromkeys(found))
    declared = getattr(route, "dependant", None)  # codespell:ignore
    pending = list(getattr(declared, "dependencies", ()))
    while pending:
        dependency = pending.pop(0)
        if getattr(dependency.call, AUTHENTICATED_MARKER, False):
            found.extend(getattr(dependency, "own_oauth_scopes", None) or ())
        pending.extend(dependency.dependencies)
    return tuple(dict.fromkeys(found))


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
        self._exclude = as_patterns(exclude, name="exclude")
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
        """Return whether the request asks for a path served without a credential."""
        if self._exclude and not selects(
            route_path(scope), include=(), exclude=self._exclude
        ):
            return True
        public = self._public
        return public is not None and public.matches(scope)

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


def _bearer_token(scope: Scope) -> str:
    """Return the bearer token the request carries.

    Raises:
        AmbiguousCredentialsError: If it carries more than one credential.
        AuthenticationRequiredError: If it carries none, or one in another
            scheme.
    """
    credentials = [
        value for name, value in scope["headers"] if name == b"authorization"
    ]
    if len(credentials) > 1:
        raise AmbiguousCredentialsError
    if not credentials:
        raise AuthenticationRequiredError
    scheme, _, token = credentials[0].decode("latin-1").partition(" ")
    if scheme.lower() != _BEARER:
        raise AuthenticationRequiredError
    return token


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

    Every request is authenticated except the paths in `exclude` and the
    routes that declare `Anonymous()`. The verifier is opened with the app,
    so its keys load before the first request and stay fresh while it
    serves.

    Nothing is read from the environment and nothing is live: every setting
    decides what the service is protected by, so each one changes with a
    deploy.

    Read more in the [Authentication](../http/authentication.md) docs.
    """

    kind: ClassVar[str] = "authenticated_requests"
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
            Doc("Registration name, for a second verifier on one app."),
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
            Doc("Registration name, for a second verifier on one app."),
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

        Called by the FastAPI integration after the middleware is added. A
        framework that builds no schema never calls it.
        """
        if not self._openapi:
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
        """
        self._public.read(app)

    def refresh_routes(
        self,
        app: Annotated[Any, Doc("The application to read the routes off.")],  # noqa: ANN401
    ) -> None:
        """Read the routes again, for a report on the app as it is now."""
        self._public.read(app)

    def handled_exceptions(self) -> tuple[type[Exception], ...]:
        """Return what this component answers rather than letting through.

        The middleware answers what it refuses itself. These are what a
        route raises, such as a missing scope, and registering the
        component is the opt-in for answering those the same way.
        """
        return (*_REFUSALS, InsufficientScopeError)

    async def __aenter__(self) -> Self:
        """Read the routes again, and open the verifier so its keys load."""
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
