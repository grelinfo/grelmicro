"""Authentication at the HTTP edge."""

from __future__ import annotations

import copy
import enum
import inspect
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    Protocol,
    Self,
    cast,
)
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, field_validator, model_validator
from typing_extensions import Doc

from grelmicro._caller import is_authenticated, subject_of
from grelmicro._config import build_config
from grelmicro._discovery import load_integration
from grelmicro._paths import (
    PathPatterns,
    as_patterns,
    path_format,
    route_path,
    route_template,
    selects,
)
from grelmicro.errors import (
    AmbiguousCredentialsError,
    AuthenticationRequiredError,
    InsufficientScopeError,
    _scope_tokens,
)
from grelmicro.http._component import (
    ErrorResponses,
    send_error,
)
from grelmicro.http._gate import (
    CROSSED_KEY,
    EXCLUDED_ROOT_KEY,
    GATE_KEY,
    PENDING_KEY,
    ROUTE_KEY,
    UNANSWERED_KEY,
    Carried,
    Crossing,
    Edge,
    GatePolicy,
    RouteGate,
    Unanswered,
    crossed,
    deny_websocket,
    edge_of,
    handed_on,
    refuse_websocket,
    wrapped,
)
from grelmicro.http._kinds import (
    AMBIGUOUS_CREDENTIALS,
    AUTHENTICATION_REQUIRED,
    CLIENT_BANNED,
    INSUFFICIENT_SCOPE,
    SIGNING_KEYS_UNAVAILABLE,
    TOKEN_REJECTED,
)
from grelmicro.http._openapi import (
    add_error_schema,
    declaration_of,
    declared_operations,
    error_format,
    operations_of,
)
from grelmicro.http._ratelimit import bucket_of
from grelmicro.http._requirement import TOKEN_SCOPE_KEY
from grelmicro.http._routes import RouteDeclaration, route_name
from grelmicro.security._events import SCOPE_KEY, SecurityEvents
from grelmicro.security.bans import ClientBannedError
from grelmicro.security.jwks import SigningKeysUnavailableError
from grelmicro.security.jwt import (
    DiscoveryConfig,
    TokenRejectedError,
    TokenRejectedReason,
)
from grelmicro.security.principal import _verified_token

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Iterable,
        Mapping,
        MutableMapping,
    )
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType

    from grelmicro.http._component import RenderedError
    from grelmicro.http._gate import Answering
    from grelmicro.security.bans import ClientBans
    from grelmicro.security.clientip import TrustedProxies
    from grelmicro.security.jwt import TokenVerifier
    from grelmicro.security.principal import Principal

    class _AwaitedVerifier(Protocol):
        """A verifier answering with an awaitable, such as one on the network."""

        def verify(self, token: str) -> Awaitable[Principal]:
            """Return the caller the token stands for, once awaited."""
            ...  # pragma: no cover

    _Verifier = TokenVerifier | _AwaitedVerifier

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

    _CallerCheck = Callable[
        [Principal, Scope], Principal | Awaitable[Principal | None] | None
    ]

__all__ = [
    "AuthenticatedRequests",
    "AuthenticatedRequestsConfig",
    "AuthenticatedRequestsMiddleware",
]

_BEARER = "bearer"
"""The scheme a bearer token travels under, lowercased for the comparison."""

_REFUSALS = (
    AmbiguousCredentialsError,
    AuthenticationRequiredError,
    ClientBannedError,
    SigningKeysUnavailableError,
    TokenRejectedError,
)
"""What the middleware answers itself, rather than letting reach the app."""

_ROUTE_CHALLENGES = (
    AmbiguousCredentialsError,
    AuthenticationRequiredError,
    InsufficientScopeError,
    TokenRejectedError,
)
"""Every bearer refusal a route may raise, which may be rendered above us."""

_REFUSAL_KINDS: Final = (
    (AuthenticationRequiredError, AUTHENTICATION_REQUIRED),
    (AmbiguousCredentialsError, AMBIGUOUS_CREDENTIALS),
    (InsufficientScopeError, INSUFFICIENT_SCOPE),
    (ClientBannedError, CLIENT_BANNED),
    (SigningKeysUnavailableError, SIGNING_KEYS_UNAVAILABLE),
)
"""The kind each error is recorded as, whose slug and status it answers with."""


def refusal_of(error: BaseException) -> tuple[str, int] | None:
    """Return the refusal `error` is recorded as and its status, or `None`.

    A rejected token is recorded by its reason, the word its `401` body
    carries. Every other refusal by the anchor of its error type.
    """
    if isinstance(error, TokenRejectedError):
        return error.reason.value, TOKEN_REJECTED.status
    for error_type, kind in _REFUSAL_KINDS:
        if isinstance(error, error_type):
            return kind.slug, kind.status
    return None


def arrived_path(scope: Scope) -> str:
    """Return the path the request arrived with, before a router rewrote it.

    Litestar takes the root path off `path` as it routes, and `raw_path`
    keeps it, so a route is named the way the access log names it.
    """
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        return unquote(raw.decode("latin-1"))
    return scope.get("path", "")


def template_under_root(template: str, scope: Scope) -> str:
    """Return a route's template, under the root path the request arrived at.

    As the access log names the route: a root path the request's path
    starts with goes on, and one a proxy stripped from it stays off.
    """
    root = scope.get("root_path", "").rstrip("/")
    if root and arrived_path(scope).startswith(root):
        return f"{root}{template}"
    return template


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


class _GatePolicies:
    """The policy the gates of each app read, held by app.

    `micro.install(app)` adds an app once its integration's
    `install_route_gate` wrapped its routes. An app it holds no policy for
    has no gate, and the middleware authenticates every request to it.
    """

    __slots__ = ("_gated",)

    def __init__(self) -> None:
        """Start with no app."""
        self._gated: dict[int, tuple[Any, GatePolicy]] = {}

    def gate(self, app: Any, policy: GatePolicy) -> None:  # noqa: ANN401
        """Hold `policy` as the one the gates of `app` read.

        The app is held, so its identity stays its own.
        """
        self._gated[id(app)] = (app, policy)

    def policy_of(self, app: Any) -> GatePolicy | None:  # noqa: ANN401
        """Return the policy the gates of `app` read, or `None` when it has none."""
        gated = self._gated.get(id(app))
        return None if gated is None else gated[1]


def route_authentication(
    app: Any,  # noqa: ANN401
) -> Callable[[str, str], tuple[bool, tuple[str, ...]]]:
    """Return what says how a route of `app` is authenticated, from its declaration.

    Asked with the route's path, as its integration declares it, and a
    method, for a report or a schema. It answers whether the route is
    served without a credential, and the scopes its declaration requires,
    sorted. A route its integration declares nothing for requires a caller
    and no scope.

    A route is served without a credential only on an app whose
    integration ships both `install_route_gate` and `route_declarations`.
    A route the integration names as served by another route declared
    before it is described as authenticated, as the router serves it.
    """
    integration = load_integration(app)
    gated = hasattr(integration, "install_route_gate") and hasattr(
        integration, "route_declarations"
    )
    declared = _Declared(_listed(app))
    shadowed = _shadowed(integration, app) if gated else frozenset()

    def read(path: str, method: str) -> tuple[bool, tuple[str, ...]]:
        declaration = declared.of(path or "/", method)
        if declaration is None:
            return False, ()
        if (
            gated
            and declaration.anonymous
            and (declaration.path, method) not in shadowed
        ):
            return True, ()
        return False, tuple(sorted(declaration.scopes))

    return read


def _listed(app: Any) -> Iterable[RouteDeclaration]:  # noqa: ANN401
    """Return the declarations the app's integration lists, none without `route_declarations`."""
    listed = getattr(load_integration(app), "route_declarations", None)
    return () if listed is None else listed(app)


def _shadowed(
    integration: object,
    app: Any,  # noqa: ANN401
) -> frozenset[tuple[str, str]]:
    """Return each anonymous route another route serves instead, by path and method.

    As the integration's optional `_shadowed_routes(app)` names them. An
    integration without it names none.
    """
    shadowed = getattr(integration, "_shadowed_routes", None)
    return frozenset(() if shadowed is None else shadowed(app))


class _Declared:
    """The declarations an integration lists, found by path and method.

    A path matches a declaration written the same way, or, when none is,
    one written without the path's converters.
    """

    __slots__ = ("_exact",)

    def __init__(self, declarations: Iterable[RouteDeclaration]) -> None:
        """Index each declaration under every method it answers."""
        self._exact: dict[tuple[str, str | None], RouteDeclaration] = {}
        for declaration in declarations:
            for method in declaration.methods or (None,):
                self._exact.setdefault((declaration.path, method), declaration)

    def of(self, path: str, method: str) -> RouteDeclaration | None:
        """Return the declaration of `method` on `path`, or `None`."""
        return self._written(path, method) or self._written(
            path_format(path), method
        )

    def _written(self, path: str, method: str) -> RouteDeclaration | None:
        """Return the declaration of `method` on `path` written as it is, or `None`."""
        return self._exact.get((path, method)) or self._exact.get((path, None))


def serves_anonymous_routes(app: Any) -> bool:  # noqa: ANN401
    """Return whether the authentication in front of `app` reads `Anonymous()`.

    One `micro.install(app)` added does. One the app added itself serves
    public paths through `exclude` alone, and `install` adds none beside it.
    An app whose integration lists neither is described as `install` would
    wire it.
    """
    found = _listed_authentication(app)
    return found is None or found[1].get("policies") is not None


def _listed_authentication(
    app: Any,  # noqa: ANN401
) -> tuple[_Placement, Mapping[str, Any]] | None:
    """Return the first authentication middleware the app's integration lists, with its arguments.

    Looked for in front of the router, through the integration's optional
    `_front_middleware(app)`, then behind it, through
    `_routed_middleware(app)`. `None` when neither lists one.
    """
    integration = load_integration(app)
    for placed, name in (
        (_Placement.IN_FRONT, "_front_middleware"),
        (_Placement.BEHIND, "_routed_middleware"),
    ):
        listed = getattr(integration, name, None)
        for middleware, options in () if listed is None else listed(app):
            if isinstance(middleware, type) and issubclass(
                middleware, AuthenticatedRequestsMiddleware
            ):
                return placed, options
    return None


def is_anonymous_declaration(call: object) -> bool:
    """Return whether a dependency is `Anonymous()`, which computes nothing.

    Resolving it first gates nothing, so a route declaring it stays as
    cacheable and as replayable as one declaring no dependency at all.
    """
    return bool(getattr(call, _ANONYMOUS_MARKER, False))


def refuse_anonymous_caller(declaration: RouteDeclaration) -> None:
    """Refuse an anonymous route that requires a caller, naming it.

    An integration calls it on a route declaring `Anonymous()` that also
    requires a caller through `Authenticated`, `CurrentPrincipal` or
    `Claims`, which refuses every request `Anonymous()` is there to serve.

    Raises:
        TypeError: Naming the route.
    """
    msg = (
        f"{route_name(declaration)} declares Anonymous() and requires a "
        f"caller through Authenticated, CurrentPrincipal or Claims, so it "
        f"refuses every request it was written to serve. Read "
        f"OptionalPrincipal on a public route, or take away what makes the "
        f"route public."
    )
    raise TypeError(msg)


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


def operation_declarations(
    app: Any,  # noqa: ANN401
    *,
    anonymous: bool,
) -> list[RouteDeclaration]:
    """Return one declaration per operation the app's integration declares.

    Whether an operation is served without a credential, and the scopes it
    needs, are read as `route_authentication` reads them, so a public route
    another route serves instead is described as authenticated.
    `precondition_required` is the one the route's own declaration carries.
    With `anonymous` false no operation is public, as for a middleware
    added by hand. An app whose integration declares no route has none.
    """
    read = route_authentication(app)
    found: list[RouteDeclaration] = []
    for declaration in _listed(app):
        # `None` answers every method, as a mount or a websocket route does.
        for method in declaration.methods or (None,):
            served, required = read(declaration.path, method or "")
            public = anonymous and served
            found.append(
                RouteDeclaration(
                    declaration.path,
                    methods=None if method is None else frozenset({method}),
                    anonymous=public,
                    scopes=frozenset(() if public else required),
                    precondition_required=declaration.precondition_required,
                )
            )
    return found


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
    metadata_path: str | None = None,
) -> None:
    """Require the scheme on every covered operation, with its refusals.

    Shared by every framework that builds a schema, so the same app is
    described the same way whichever one serves it. The protected resource
    metadata, when published, is described as an operation needing nothing,
    and stays so however many times a cached schema is annotated again.
    """
    ref = add_error_schema(schema, model)
    content = {media_type: {"schema": {"$ref": ref}}} if ref else {}
    schema.setdefault("components", {}).setdefault(
        "securitySchemes", {}
    ).setdefault(SECURITY_SCHEME, security_scheme(verifier))
    inherited = schema.get("security")
    for path, item in (schema.get("paths") or {}).items():
        if path == metadata_path:
            continue
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
    if metadata_path is not None:
        paths = schema["paths"] = schema.get("paths") or {}
        paths.setdefault(
            metadata_path, {"get": copy.deepcopy(_METADATA_OPERATION)}
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
            "Exact match unless the pattern ends with `/*`, which matches as "
            "a prefix."
        ),
    ] = ()
    resource: Annotated[
        str | None,
        Doc(
            "The URL clients call the service at, as they see it behind any "
            "proxy. Publishes the service's protected resource metadata and "
            "adds `resource_metadata` to every bearer challenge. An `https` "
            "URL with no fragment."
        ),
    ] = None
    authorization_servers: Annotated[
        tuple[str, ...],
        Doc(
            "Issuers a client gets a token from, listed in the metadata. "
            "Left empty, the verifier's issuers are listed."
        ),
    ] = ()
    scopes: Annotated[
        tuple[str, ...],
        Doc(
            "Scopes a client may ask for, listed in the metadata. Left "
            "empty, none are listed."
        ),
    ] = ()
    enduser: Annotated[
        bool,
        Doc(
            "Whether the server span and the security events name the "
            "caller as `enduser.id`, the subject of a token whose signature "
            "verified."
        ),
    ] = False

    @field_validator("resource")
    @classmethod
    def _check_resource(cls, value: str | None) -> str | None:
        """Refuse a resource a client could not call, quote or route to."""
        if value is not None and (
            not _is_url(value, query=True) or _holds_braces(value)
        ):
            msg = (
                "resource must be an https URL with no fragment, and no brace "
                "in its path, which a router reads as a path parameter"
            )
            raise ValueError(msg)
        return value

    @field_validator("authorization_servers")
    @classmethod
    def _check_authorization_servers(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Refuse an authorization server that is not an issuer URL."""
        if not all(_is_url(server, query=False) for server in value):
            msg = (
                "authorization_servers must be https URLs with no query or "
                "fragment"
            )
            raise ValueError(msg)
        return value

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse a scope that is not an OAuth scope token."""
        return _scope_tokens(value)

    @model_validator(mode="after")
    def _check_described_resource(self) -> Self:
        """Refuse what describes the metadata when nothing publishes it."""
        if self.resource is None and (
            self.authorization_servers or self.scopes
        ):
            msg = (
                "authorization_servers and scopes describe the metadata "
                "resource publishes, so they need resource"
            )
            raise ValueError(msg)
        return self


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
    - A caller `check` refuses: `401`, with the reason `revoked`.
    - More than one credential: `400`.
    - A caller `bans` refuses: `429`, with `Retry-After`.
    - Keys that have not loaded: `503`.

    Each is rendered by the app's `ErrorResponses`, with the
    `WWW-Authenticate` challenge RFC 6750 gives it, and written as a
    security event on `grelmicro.security.events`.

    With `resource=`, it serves the service's protected resource metadata,
    RFC 9728, at `/.well-known/oauth-protected-resource` followed by the
    path of `resource`, to any caller. Every bearer challenge the app
    answers with then carries `resource_metadata`, pointing at it.

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

    def __init__(  # noqa: PLR0913
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        verifier: Annotated[
            _Verifier,
            Doc(
                "Verifies each bearer token, such as a `JWTVerifier`. Its"
                " `verify` may answer with the caller or an awaitable of it."
            ),
        ],
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths served without a credential. Exact match unless the "
                "pattern ends with `/*`, which matches as a prefix."
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
        check: Annotated[
            _CallerCheck | None,
            Doc(
                "Checks each verified caller before the app sees it, such as "
                "against the tokens the service revoked. Called with the "
                "caller and the request's ASGI scope, it returns the caller "
                "the app receives, or `None` to refuse it. It may answer at "
                "once or with an awaitable."
            ),
        ] = None,
        resource: Annotated[
            str | None,
            Doc(
                "The URL clients call the service at, as they see it behind "
                "any proxy. Publishes its protected resource metadata."
            ),
        ] = None,
        authorization_servers: Annotated[
            tuple[str, ...] | list[str],
            Doc("Issuers listed in the metadata. Left empty, the verifier's."),
        ] = (),
        scopes: Annotated[
            tuple[str, ...] | list[str],
            Doc("Scopes listed in the metadata. Left empty, none are."),
        ] = (),
        enduser: Annotated[
            bool,
            Doc(
                "Name the caller as `enduser.id` on the server span and on "
                "the security events. Off by default, because a subject can "
                "be personal data."
            ),
        ] = False,
        policies: Annotated[
            _GatePolicies | None,
            Doc(
                "The policy the gates of each app read, filled by "
                "`micro.install(app)`, whose route declarations say which "
                "routes serve a caller with no credential. A middleware "
                "added by hand serves public paths through `exclude` "
                "instead."
            ),
        ] = None,
    ) -> None:
        """Initialize the middleware with the verifier it trusts.

        Raises:
            TypeError: If `exclude`, `authorization_servers` or `scopes` is
                a single string, `bans` is given without `trusted`, `check`
                cannot be called, or `resource` is given and no
                authorization server is known.
            ValueError: If a pattern in `exclude` matches every path, or is
                a prefix ending inside a path segment, such as `/public*`.
            SettingsValidationError: If `resource`, an authorization server
                or a scope is not one a client could use.
        """
        if bans is not None and trusted is None:
            msg = (
                "AuthenticatedRequestsMiddleware needs trusted= to resolve "
                "the caller a ban is counted against. Without it the only "
                "address left is the socket peer, which behind an ingress "
                "is the ingress, and one forged token would ban everyone."
            )
            raise TypeError(msg)
        if check is not None and not callable(check):
            msg = (
                f"check= takes a function of the caller and the scope, and "
                f"was given a {type(check).__name__}."
            )
            raise TypeError(msg)
        self.app = app
        self._verifier = verifier
        self._check = check
        self._exclude = _bounded(as_patterns(exclude, name="exclude"))
        self._bans = bans
        self._trusted = trusted
        self._policies = policies
        described = build_config(
            AuthenticatedRequestsConfig,
            resource=resource,
            authorization_servers=_names(
                authorization_servers, name="authorization_servers"
            ),
            scopes=_names(scopes, name="scopes"),
            enduser=enduser,
        )
        self._events = SecurityEvents(enduser=described.enduser)
        # Bound once, so a request leaves it on the scope without building
        # a new bound method each time.
        self._record_route_refusal = self._record_raised
        self._record_unauthenticated = self._record
        self._policy: object = _UNREAD
        self._edge: Edge | None = None
        self._below: ASGIApp = app
        self._metadata = _resource_metadata(
            described.resource,
            described.authorization_servers,
            described.scopes,
            verifier,
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Authenticate the request, then serve or refuse it."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        metadata = self._metadata
        if metadata is not None:
            if scope["type"] == "http" and route_path(scope) in metadata.paths:
                await metadata.serve(scope, send)
                return
            send = _pointing_at(send, metadata.pointer)
        unverified = self._unverified(scope)
        if unverified is not None:
            await unverified(scope, receive, send)
            return
        try:
            caller, token = await self._authenticate(scope)
        except _REFUSALS as error:
            self._record(scope, error)
            await _refuse(scope, receive, send, error)
            return
        verified = _verified_token(token, _expiry_of(caller))
        check = self._check
        if check is not None:
            try:
                caller = await _checked(check, caller, scope)
            except Exception as error:  # noqa: BLE001 - rendered or re-raised
                self._record(scope, error, caller=caller)
                await _refuse(scope, receive, send, error)
                return
        scope["user"] = caller
        scope["auth"] = caller
        scope[SCOPE_KEY] = self._record_route_refusal
        scope[TOKEN_SCOPE_KEY] = verified
        if self._policy is not None:
            self._pass_on(scope)
        self._events.authenticated(caller)
        await self._forward(scope, receive, send)

    def _record_raised(self, scope: Scope, error: BaseException) -> None:
        """Record a refusal a route raised after this middleware authenticated it."""
        self._record(scope, error, caller=scope.get("user"), authenticated=True)

    def _record(
        self,
        scope: Scope,
        error: BaseException,
        *,
        caller: object = None,
        authenticated: bool = False,
    ) -> None:
        """Record a refusal, when it is one.

        A gate names the route it refused on, and a request no route took
        names none. Otherwise the route is read the way the router records
        it. On an installed app, a refusal before routing names the route
        the request would reach, as the app's route reader matches it. The
        route is left in the scope for the access record to name. A
        refusal `check` or a route raised names the `caller` its token
        verified. `authenticated` says the request already counted as one
        that authenticated.
        """
        found = refusal_of(error)
        if found is None:
            return
        if ROUTE_KEY not in scope:
            scope[ROUTE_KEY] = route_template(scope)
        self._events.refused(
            scope,
            refusal=found[0],
            status=found[1],
            template=scope[ROUTE_KEY],
            subject=getattr(error, "subject", None) or subject_of(caller),
            authenticated=authenticated,
        )

    async def _forward(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Run the app, pointing a refusal it raises at the metadata.

        A refusal a route raises is rendered wherever the framework catches
        it. Where that is above this middleware, out of reach of the `send`
        it wraps, the refusal itself carries the URL instead.

        On an app whose routes carry a gate, the request goes on past the
        answering middleware, which run at the route it reaches. What one
        of them raises there is raised again here, where it would have
        been raised before routing.
        """
        pending = scope.pop(PENDING_KEY, None)
        if pending is not None:
            refusal = pending(scope)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        edge = self._edge
        metadata = self._metadata
        try:
            await self._below(scope, receive, send)
        except _ROUTE_CHALLENGES as error:
            if metadata is not None:
                error.resource_metadata = metadata.url
            raise
        except Carried as carried:
            if carried.edge is not edge:
                raise
            raised = carried.error
        else:
            return
        raise raised

    def _pass_to(self, policy: GatePolicy) -> None:
        """Pass what the app's gates admit on past the answering middleware.

        In front of the router, they run in the lane of each route. Behind
        it, they run here, once the route's check admitted the request.
        """
        if policy.behind:
            self._below = wrapped(handed_on(self.app), policy.answering)
            return
        self._below, self._edge = edge_of(self.app, policy)

    def _pass_on(self, scope: Scope) -> None:
        """Leave the policy of the app's gates on the request, and the edge it crossed."""
        scope[GATE_KEY] = self._policy_of(scope)
        edge = self._edge
        if edge is not None:
            crossed(scope, edge)

    def _unverified(self, scope: Scope) -> ASGIApp | None:
        """Return how the request is served without verifying a credential.

        An excluded path never reads one. On an app whose routes carry a
        gate, a request sending no bearer token is routed for the gate it
        reaches to answer when a route serves a caller with none, answered
        by that gate when this middleware runs behind the router, and
        refused before routing otherwise. On any other app every request
        is authenticated. `None` for a request whose credential is verified
        or refused.
        """
        if self._exclude and not selects(
            route_path(scope), include=(), exclude=self._exclude
        ):
            return self._serve_excluded
        if self._policies is None or _presents_bearer(scope):
            return None
        policy = self._policy_of(scope)
        if policy is not None:
            if policy.behind:
                return self._serve_anonymous if PENDING_KEY in scope else None
            return self._serve_routed if policy.defers else None
        return None

    def _policy_of(self, scope: Scope) -> GatePolicy | None:
        """Return the policy the gates of the app serving the request read.

        Read once, off the app the middleware sits in front of, which is the
        one in `scope["app"]` when the request reaches it.
        """
        policy = self._policy
        if policy is _UNREAD:
            policies = self._policies
            policy = self._policy = (
                None
                if policies is None
                else policies.policy_of(scope.get("app"))
            )
            if policy is not None:
                self._pass_to(policy)
        return cast("GatePolicy | None", policy)

    async def _serve_anonymous(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Serve a request without a credential, with a caller that is not one.

        Behind the router, once a route was reached, whose check decides.
        The caller is set only when nothing else set one, so an
        authentication the app runs itself outside this one keeps its
        caller.
        """
        scope.setdefault("user", _ANONYMOUS)
        scope.setdefault("auth", _ANONYMOUS)
        scope[GATE_KEY] = self._policy_of(scope)
        scope[SCOPE_KEY] = self._record_unauthenticated
        await self._forward(scope, receive, send)

    async def _serve_routed(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Route a request without a credential, for a gate of the app to answer.

        What the app answers before a gate admitted or refused the request,
        such as a `404`, a `405`, a redirect or an exception, is answered
        with the `401` a protected route answers, recorded with no route.
        A request the same app routed already is answered where it was
        routed, and so is one an app it is mounted in routed and no gate
        answered yet.
        """
        scope.setdefault("user", _ANONYMOUS)
        scope.setdefault("auth", _ANONYMOUS)
        scope[SCOPE_KEY] = self._record_unauthenticated
        routed_by = scope.get(GATE_KEY)
        self._pass_on(scope)
        outer: Unanswered | None = scope.get(UNANSWERED_KEY)
        if outer is not None and (outer.open or routed_by is scope[GATE_KEY]):
            await self._forward(scope, receive, send)
            return
        unanswered = scope[UNANSWERED_KEY] = Unanswered()
        policy: GatePolicy = scope[GATE_KEY]
        arrived: Crossing = scope[CROSSED_KEY]
        preflight = _is_preflight(scope)
        replaced = False

        async def guarded(message: Message) -> None:
            nonlocal replaced
            if replaced:
                return
            if (
                unanswered.open
                and message["type"] in _ANSWERS
                and not (preflight and _allows_origin(message))
            ):
                replaced = True
                await self._refuse_unanswered(scope, send, policy, arrived)
                return
            await send(message)

        try:
            await self._forward(scope, receive, guarded)
        except Exception:
            if not unanswered.open or replaced:
                raise
            await self._refuse_unanswered(
                scope, send, policy, arrived, receive=receive
            )

    async def _refuse_unanswered(
        self,
        scope: Scope,
        send: Send,
        policy: GatePolicy,
        arrived: Crossing,
        *,
        receive: Receive | None = None,
    ) -> None:
        """Answer a request no gate answered with the `401` of a protected route.

        Rendered by `policy`, for the path the request `arrived` with at
        this app, whichever app it was routed into since. A websocket
        handshake is refused with the denial, reading the handshake first
        when `receive` is given, since nothing read it yet.
        """
        scope[ROUTE_KEY] = None
        error = AuthenticationRequiredError()
        self._record(scope, error)
        rendered = cast(
            "RenderedError",
            policy.errors.render(error, instance=arrived.path),
        )
        if scope["type"] == "http":
            await send_error(send, rendered)
        elif receive is not None:
            await deny_websocket(scope, receive, send, rendered)
        else:
            await refuse_websocket(scope, send, rendered)

    async def _serve_excluded(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Serve a request to an excluded path, never reading its credential.

        On an app whose routes carry a gate, the gate the request reaches
        holds it to the path it was routed with, read from the root path it
        arrived with.
        """
        policy = self._policy_of(scope)
        if policy is not None:
            scope[EXCLUDED_ROOT_KEY] = scope.get("root_path", "")
            scope[SCOPE_KEY] = self._record_unauthenticated
            self._pass_on(scope)
        scope.setdefault("user", _ANONYMOUS)
        scope.setdefault("auth", _ANONYMOUS)
        await self._forward(scope, receive, send)

    async def _authenticate(self, scope: Scope) -> tuple[Principal, str]:
        """Return the verified caller and its token, or raise what the caller is told."""
        token = _bearer_token(scope)
        client = self._client_of(scope)
        bans = self._bans
        if client is not None and bans is not None and bans.banned(client):
            raise ClientBannedError(retry_after=bans.banned_for(client))
        try:
            return await self._verified(token), token
        except TokenRejectedError as error:
            if client is not None and bans is not None:
                bans.record(client, error.reason)
            raise

    async def _verified(self, token: str) -> Principal:
        """Verify `token`, waiting for one refresh when it names a new key.

        A verifier answering with an awaitable, such as one asking the
        authorization server, is awaited, and so is its answer after the
        refresh. One answering at once costs no await.
        """
        verifier = self._verifier
        try:
            # Either the caller or an awaitable of it, whichever it answers.
            caller: Any = verifier.verify(token)
            if inspect.isawaitable(caller):
                caller = await caller
        except TokenRejectedError as error:
            if error.reason is not TokenRejectedReason.UNKNOWN_KEY:
                raise
            if not await self._refreshed():
                raise
        else:
            return caller
        retried: Any = verifier.verify(token)
        if inspect.isawaitable(retried):
            retried = await retried
        return retried

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


def _expiry_of(caller: object) -> int | None:
    """Return when the caller's token expires, when its verifier says.

    Read off `expires_at` in seconds since the epoch, as `JWTClaims` carries
    it. A caller without one, or with anything but whole seconds, has none.
    """
    expires_at = getattr(caller, "expires_at", None)
    if isinstance(expires_at, int) and not isinstance(expires_at, bool):
        return expires_at
    return None


def _bounded(exclude: tuple[str, ...]) -> tuple[str, ...]:
    """Return `exclude`, refusing a pattern that covers more than it names.

    A prefix pattern matches every path that starts with it, so one that
    stops inside a segment, such as `/public*`, also serves `/publicity`.
    A prefix ends at a segment boundary, `/public/*`, or the pattern is an
    exact path.

    Raises:
        ValueError: If a pattern is `*` or `/*`, which would serve every
            request without a credential, or a prefix ending inside a
            path segment.
    """
    for pattern in exclude:
        if pattern in _EVERY_PATH:
            msg = (
                f"exclude={exclude!r} matches every path, so no request would "
                f"be authenticated. Leave AuthenticatedRequests unregistered "
                f"to serve the app without a credential."
            )
            raise ValueError(msg)
        if pattern.endswith("*") and not pattern[:-1].endswith("/"):
            raise ValueError(_mid_segment(pattern))
    return exclude


def _mid_segment(pattern: str) -> str:
    """Say why a prefix ending inside a segment is refused, and what to write."""
    base = pattern.rstrip("*").rstrip("/")
    msg = (
        f"exclude pattern {pattern!r} ends inside a path segment, so it also "
        f"serves every path that merely starts the same way without a "
        f"credential. Write a prefix that ends at a segment boundary"
    )
    if not base:
        return f"{msg}, such as '/internal/*'."
    return (
        f"{msg}: '{base}/*' for everything under it, or '{base}' for that "
        f"path alone."
    )


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


async def _checked(
    check: _CallerCheck, caller: Principal, scope: Scope
) -> Principal:
    """Return the caller `check` accepts, awaiting it when it answers later.

    Raises:
        TokenRejectedError: With `revoked`, if `check` refuses the caller.
        TypeError: If `check` answers with something that is not an
            authenticated caller, which would otherwise reach the app as one.
    """
    checked: Any = check(caller, scope)
    if inspect.isawaitable(checked):
        checked = await checked
    if checked is None:
        raise TokenRejectedError(
            TokenRejectedReason.REVOKED, subject=subject_of(caller)
        )
    if not is_authenticated(checked):
        msg = (
            f"check= answered with a {type(checked).__name__}, which is not "
            f"an authenticated caller. Return the caller, or None to refuse "
            f"it."
        )
        raise TypeError(msg)
    return checked


def _errors_of(app: object) -> ErrorResponses:
    """Return the `ErrorResponses` the app answers every refusal with."""
    registered = getattr(
        getattr(app, "state", None), "grelmicro_error_responses", None
    )
    return registered if registered is not None else ErrorResponses()


async def _refuse(
    scope: Scope, receive: Receive, send: Send, error: Exception
) -> None:
    """Answer a refusal in the format the app answers every refusal with.

    An error with no kind of its own, such as one a `check` raised, is
    raised again, for the server to answer as any other failure.
    """
    rendered = _errors_of(scope.get("app")).render(
        error, instance=scope.get("path")
    )
    if rendered is None:
        raise error
    if scope["type"] == "http":
        await send_error(send, rendered)
        return
    await deny_websocket(scope, receive, send, rendered)


_ANSWERS: Final = frozenset(
    {
        "http.response.start",
        "websocket.accept",
        "websocket.close",
        "websocket.http.response.start",
    }
)
"""The messages that start the answer to a request or a handshake."""


class _Placement(enum.Enum):
    """Where an app runs this component's authentication."""

    IN_FRONT = enum.auto()
    """Before the app routes a request."""

    BEHIND = enum.auto()
    """Once the app routed a request, as a middleware of its route."""


_PREFLIGHT_HEADERS: Final = frozenset(
    {b"origin", b"access-control-request-method"}
)
"""The headers a browser sends on a CORS preflight."""

_ALLOWS_ORIGIN: Final = b"access-control-allow-origin"
"""The header a CORS answer carries."""


def _is_preflight(scope: Scope) -> bool:
    """Return whether the request is a CORS preflight, which carries no credential."""
    return (
        scope.get("method") == "OPTIONS"
        and {name for name, _ in scope["headers"]} >= _PREFLIGHT_HEADERS
    )


def _allows_origin(message: Message) -> bool:
    """Return whether a response carries a CORS answer."""
    return any(
        name.lower() == _ALLOWS_ORIGIN for name, _ in message.get("headers", ())
    )


_UNREAD: Final = object()
"""The policy of a middleware that has not read its app's yet."""


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

    def __init__(  # noqa: PLR0913
        self,
        verifier: Annotated[
            _Verifier,
            Doc(
                "Verifies each bearer token, such as a `JWTVerifier`. Its"
                " `verify` may answer with the caller or an awaitable of it."
            ),
        ],
        *,
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths served without a credential, such as health probes. "
                "Exact match unless the pattern ends with `/*`, which matches "
                "as a prefix."
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
        check: Annotated[
            _CallerCheck | None,
            Doc(
                "Checks every verified caller before the app sees it, such as "
                "against the tokens the service revoked. Called with the "
                "caller and the request's ASGI scope, it returns the caller "
                "routes receive, or `None` to refuse it `401` with the reason "
                "`revoked`. It runs for a cached token and a websocket "
                "handshake too, and its answer is never cached."
            ),
        ] = None,
        resource: Annotated[
            str | None,
            Doc(
                "The URL clients call the service at, as they see it behind "
                "any proxy, such as `https://api.example.com/orders`. "
                "Publishes the service's protected resource metadata, so a "
                "client learns where to get a token from the URL alone."
            ),
        ] = None,
        authorization_servers: Annotated[
            tuple[str, ...] | list[str],
            Doc(
                "Issuers a client gets a token from, listed in the metadata. "
                "Left empty, the verifier's issuers are listed."
            ),
        ] = (),
        scopes: Annotated[
            tuple[str, ...] | list[str],
            Doc(
                "Scopes a client may ask for, listed in the metadata. Left "
                "empty, none are listed, because the document is public."
            ),
        ] = (),
        enduser: Annotated[
            bool,
            Doc(
                "Name the caller as `enduser.id` on the server span and on "
                "the security events: the subject of a token whose signature "
                "verified. Off by default, because a subject can be personal "
                "data."
            ),
        ] = False,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
        openapi: Annotated[
            bool,
            Doc(
                "Describe the security scheme, and the `401` and `403` a "
                "covered operation answers, in the OpenAPI schema."
            ),
        ] = True,
    ) -> None:
        """Authenticate every request through the registered middleware.

        Raises:
            TypeError: If `exclude`, `authorization_servers` or `scopes` is
                a single string, `bans` is given without `trusted`, `check`
                cannot be called, or `resource` is given and no
                authorization server is known.
            ValueError: If a pattern in `exclude` matches every path, or is
                a prefix ending inside a path segment, such as `/public*`.
            SettingsValidationError: If `resource`, an authorization server
                or a scope is not one a client could use.
        """
        config = build_config(
            AuthenticatedRequestsConfig,
            exclude=as_patterns(exclude, name="exclude"),
            resource=resource,
            authorization_servers=_names(
                authorization_servers, name="authorization_servers"
            ),
            scopes=_names(scopes, name="scopes"),
            enduser=enduser,
        )
        self._setup(
            config,
            verifier=verifier,
            bans=bans,
            trusted=trusted,
            check=check,
            name=name,
            openapi=openapi,
        )

    @classmethod
    def from_config(
        cls,
        verifier: Annotated[
            _Verifier,
            Doc(
                "Verifies each bearer token, such as a `JWTVerifier`. Its"
                " `verify` may answer with the caller or an awaitable of it."
            ),
        ],
        config: Annotated[
            AuthenticatedRequestsConfig,
            Doc("The pre-built authenticated requests configuration."),
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
        check: Annotated[
            _CallerCheck | None,
            Doc(
                "Checks every verified caller before the app sees it. It "
                "returns the caller routes receive, or `None` to refuse it."
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
                "covered operation answers, in the OpenAPI schema."
            ),
        ] = True,
    ) -> Self:
        """Build the component from a configuration that is already whole.

        The one declarative door. The verifier and the check stay beside
        the config, because they are objects rather than settings.

        Raises:
            TypeError: If a config is passed where the verifier goes.
        """
        if isinstance(verifier, AuthenticatedRequestsConfig):
            msg = (
                "AuthenticatedRequests.from_config takes the verifier first:"
                " pass from_config(verifier, config)"
            )
            raise TypeError(msg)
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            config,
            verifier=verifier,
            bans=bans,
            trusted=trusted,
            check=check,
            name=name,
            openapi=openapi,
        )
        return instance

    def _setup(
        self,
        config: AuthenticatedRequestsConfig,
        *,
        verifier: _Verifier,
        bans: ClientBans | None,
        trusted: TrustedProxies | None,
        check: _CallerCheck | None,
        name: str,
        openapi: bool,
    ) -> None:
        """Hold the configuration and the objects the middleware reads."""
        self._config = config
        self._verifier = verifier
        self._bans = bans
        self._trusted = trusted
        self._check = check
        self._name = name
        self._openapi = openapi
        self._stack: AsyncExitStack | None = None
        self._policies = _GatePolicies()
        self._gates: list[RouteGate] = []
        self._installed: dict[Any, None] = {}
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
            "check": self._check,
            "policies": self._policies,
            "resource": self._config.resource,
            "authorization_servers": self._config.authorization_servers,
            "scopes": self._config.scopes,
            "enduser": self._config.enduser,
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
    def verifier(self) -> _Verifier:
        """Return the verifier every token is checked against."""
        return self._verifier

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with."""
        return AuthenticatedRequestsMiddleware, self._options()

    def _document_openapi(
        self,
        schema: Annotated[
            dict[str, Any],
            Doc("The OpenAPI schema to describe the component in."),
        ],
        *,
        routes: Annotated[
            Iterable[RouteDeclaration],
            Doc(
                "The app's route declarations. A route declaring "
                "`anonymous` lists the scheme as optional, and one naming "
                "`scopes` requires them."
            ),
        ] = (),
        errors: Annotated[
            ErrorResponses | None,
            Doc(
                "The registered format refusals are answered in. `None` "
                "publishes RFC 9457 problem details."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Describe the security scheme and its refusals, and return the schema.

        The scheme is published, and required on every operation outside
        `exclude`, with the scopes its route declares. Each gains the `401`
        it answers, the `403` where scopes are required, and the `429` when
        `bans` is set. An operation a route declares `anonymous` lists the
        scheme as optional. The schema is edited in place, and a second
        call changes nothing. With `openapi=False` it is returned untouched.

        `micro.install(app)` calls it when FastAPI or Litestar builds the
        schema.
        """
        if not self._openapi:
            return schema
        media_type, model = error_format(errors)
        declared = declared_operations(routes)
        public: set[tuple[str, str]] = set()
        scopes: dict[tuple[str, str], list[str]] = {}
        for path, _item, _operation, method in operations_of(schema):
            found = declaration_of(declared, path, method)
            if found is None:
                continue
            if found.anonymous:
                public.add((path, method))
            else:
                scopes[path, method] = sorted(found.scopes)
        options = self._options()
        document_operations(
            schema,
            verifier=options["verifier"],
            bans=options["bans"] is not None,
            exclude=tuple(options["exclude"]),
            public=public,
            scopes=scopes,
            media_type=media_type,
            model=model,
            metadata_path=metadata_path_of(options),
        )
        return schema

    def read_routes(
        self,
        app: Annotated[Any, Doc("The application to read the routes off.")],  # noqa: ANN401
    ) -> None:
        """Check the routes the app declares, now and when it starts.

        Called by the integration after the middleware is added. The app is
        checked again when it starts, so a route added between the two
        counts as well.

        Raises:
            TypeError: If a route sits where `resource=` publishes the
                metadata, where it would never run.
        """
        refuse_routes_at_metadata(app, resource_metadata_of(self._options()))
        self._installed[app] = None

    def route_gate(
        self,
        app: Annotated[Any, Doc("The application whose routes to gate.")],  # noqa: ANN401
        errors: Annotated[
            ErrorResponses | None,
            Doc("The registered format refusals are answered in, if any."),
        ] = None,
        answering: Annotated[
            Answering,
            Doc(
                "Each registered middleware that may answer a request "
                "itself, as its class and arguments, in the order the "
                "integration placed them."
            ),
        ] = (),
        routed: Annotated[
            Iterable[tuple[Any, Mapping[str, Any]]],
            Doc(
                "The middleware the app runs once its router matched a "
                "route, as class and arguments, when its integration says."
            ),
        ] = (),
    ) -> RouteGate | None:
        """Return the gate the app's routes are wrapped with.

        Called by `micro.install(app)` once the middleware is added, for an
        integration that declares its routes. `None` when the middleware in
        front of the app is not this component's, such as one the app added
        by hand, which serves public paths through `exclude` alone. When
        the app runs this component's middleware behind its router, an
        answering middleware the app runs there too is left to it.
        """
        routed = tuple(routed)
        placed = self._placement(app)
        if placed is None:
            return None
        if placed is _Placement.BEHIND:
            declared = {middleware for middleware, _ in routed}
            answering = tuple(
                spec for spec in answering if spec[0] not in declared
            )
        gate = RouteGate(
            app,
            exclude=self._config.exclude,
            errors=errors if errors is not None else ErrorResponses(),
            policies=self._policies,
            answering=answering,
            behind=placed is _Placement.BEHIND,
        )
        self._gates.append(gate)
        return gate

    def _placement(self, app: Any) -> _Placement | None:  # noqa: ANN401
        """Return where this component's middleware sits in front of `app`.

        Read from the app's integration, never off the app. The first
        authentication middleware the integration lists decides: in front
        of the router or behind it when it is this component's, `None`
        when it is another, such as one the app added by hand. With none
        listed, the integration's `install_middleware` added this
        component's in front of the router.
        """
        found = _listed_authentication(app)
        if found is not None:
            placed, options = found
            return placed if options.get("policies") is self._policies else None
        return _Placement.IN_FRONT

    def handled_exceptions(self) -> tuple[type[Exception], ...]:
        """Return what this component answers rather than letting through.

        The middleware answers what it refuses itself. These are what a
        route raises, such as a missing scope, and registering the
        component is the opt-in for answering those the same way.
        """
        return (*_REFUSALS, InsufficientScopeError)

    async def __aenter__(self) -> Self:
        """Check the routes again, and open the verifier so its keys load.

        Raises:
            TypeError: If a route added since install sits where `resource=`
                publishes the metadata, or its integration refuses what it
                declares.
            RuntimeError: If a route the integration lists carries no gate.
        """
        metadata = resource_metadata_of(self._options())
        for app in self._installed:
            refuse_routes_at_metadata(app, metadata)
        for gate in self._gates:
            gate.refuse_ungated()
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


_WELL_KNOWN: Final = "/.well-known/oauth-protected-resource"
"""Where protected resource metadata is served, ahead of the resource's path."""

_METADATA_MAX_AGE: Final = 3600
"""Seconds a client may keep the metadata document."""

_METADATA_METHODS: Final = b"GET, HEAD, OPTIONS"
"""The methods the metadata document answers."""

_HEADER_SAFE: Final = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")
"""Printable ASCII with no space, no double quote and no backslash.

What lets a URL sit inside a quoted `WWW-Authenticate` parameter.
"""

_SINGLE_BEARER: Final = re.compile(
    rb'(?i:bearer)(?: [\w.~+-]+="[^"\\]*"(?:, ?[\w.~+-]+="[^"\\]*")*)?'
)
"""One bearer challenge whose parameters are all quoted, as every one of ours is."""

_RESPONSE_STARTS: Final = frozenset(
    {"http.response.start", "websocket.http.response.start"}
)
"""The messages that carry a response's headers."""

_METADATA_OPERATION: Final = {
    "summary": "Protected resource metadata",
    "description": (
        "Where to get a token for this service, and the scopes it offers."
    ),
    "operationId": "protected_resource_metadata",
    "security": [],
    "responses": {
        "200": {
            "description": "The protected resource metadata.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["resource"],
                        "properties": {
                            "resource": {"type": "string", "format": "uri"},
                            "authorization_servers": {
                                "type": "array",
                                "items": {"type": "string", "format": "uri"},
                            },
                            "scopes_supported": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "bearer_methods_supported": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    }
                }
            },
        }
    },
}
"""How the metadata document is described in an OpenAPI schema."""


@dataclass(frozen=True, slots=True)
class _ResourceMetadata:
    """The protected resource metadata a service publishes, ready to serve."""

    route: str
    """The path a router routes the document at: decoded, with no trailing slash."""
    paths: frozenset[str]
    """Every route path the document is served at, however the router reads it."""
    url: str
    """The URL a bearer challenge points at."""
    pointer: bytes
    """The `resource_metadata` parameter a bearer challenge gets."""
    body: bytes
    """The document itself."""

    async def serve(self, scope: Scope, send: Send) -> None:
        """Answer a request for the document, whoever asks.

        It is public by definition, so no credential is read, and a browser
        client on any origin may read it, with whatever headers its
        preflight asks to send.
        """
        method = scope.get("method")
        headers = [(b"access-control-allow-origin", b"*")]
        if method in ("GET", "HEAD"):
            status, body = 200, self.body
            headers += [
                (b"content-type", b"application/json"),
                (
                    b"cache-control",
                    f"public, max-age={_METADATA_MAX_AGE}".encode(),
                ),
                (b"content-length", str(len(body)).encode()),
            ]
        elif method == "OPTIONS":
            status, body = 204, b""
            headers += [
                (b"access-control-allow-methods", _METADATA_METHODS),
                (b"allow", _METADATA_METHODS),
                *(
                    (b"access-control-allow-headers", value)
                    for name, value in scope["headers"]
                    if name == b"access-control-request-headers"
                ),
            ]
        else:
            status, body = 405, b""
            headers += [
                (b"allow", _METADATA_METHODS),
                (b"content-length", b"0"),
            ]
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if method == "HEAD" else body,
            }
        )

    def document(self) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
        """Return an ASGI app serving the document, for a router to route to it.

        A websocket is closed, since the document is not served over one.
        """

        async def protected_resource_metadata(
            scope: Scope,
            receive: Receive,  # noqa: ARG001
            send: Send,
        ) -> None:
            if scope["type"] != "http":
                await send({"type": "websocket.close"})
                return
            await self.serve(scope, send)

        setattr(protected_resource_metadata, METADATA_MARKER, True)
        return protected_resource_metadata


def metadata_path_of(options: Mapping[str, Any]) -> str | None:
    """Return where a middleware built with `options` serves its metadata."""
    resource = options.get("resource")
    return None if resource is None else _metadata_path(resource)


def resource_metadata_of(
    options: Mapping[str, Any],
) -> _ResourceMetadata | None:
    """Return the metadata a middleware built with `options` publishes."""
    return _resource_metadata(
        options.get("resource"),
        tuple(options.get("authorization_servers") or ()),
        tuple(options.get("scopes") or ()),
        options["verifier"],
    )


def _metadata_path(resource: str) -> str:
    """Return the path the protected resource metadata of `resource` is served at.

    The well-known suffix goes between the host and the path, and a path
    that is only `/` is dropped first, so a client finds the document from
    the URL it calls.
    """
    path = urlsplit(resource).path
    return f"{_WELL_KNOWN}{'' if path == '/' else path}"


def _resource_metadata(
    resource: str | None,
    authorization_servers: tuple[str, ...],
    scopes: tuple[str, ...],
    verifier: object,
) -> _ResourceMetadata | None:
    """Build the document `resource` publishes, and where it is served.

    The URL a challenge points at keeps the resource's path as written. A
    request arrives with its path decoded, so the document is matched by
    the decoded path, and a resource path holding `%20` is still found. A
    router that drops a trailing slash routes it without one.

    Raises:
        TypeError: If no authorization server is named and the verifier
            names no issuer a client could use.
    """
    if resource is None:
        return None
    issuers = getattr(getattr(verifier, "config", None), "issuer", None) or ()
    servers = authorization_servers or tuple(issuers)
    if not servers or not all(
        _is_url(server, query=False) for server in servers
    ):
        msg = (
            "resource= publishes where a client gets a token, and the verifier "
            "names no issuer that is an https URL. Pass "
            "authorization_servers= with the issuers your tokens come from."
        )
        raise TypeError(msg)
    parts = urlsplit(resource)
    path = _metadata_path(resource)
    served = unquote(path)
    route = served.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    document: dict[str, Any] = {
        "resource": resource,
        "authorization_servers": list(servers),
    }
    if scopes:
        document["scopes_supported"] = list(scopes)
    document["bearer_methods_supported"] = ["header"]
    url = f"{parts.scheme}://{parts.netloc}{path}{query}"
    return _ResourceMetadata(
        route=route,
        paths=frozenset({served, route}),
        url=url,
        pointer=f'resource_metadata="{url}"'.encode("ascii"),
        body=json.dumps(document, separators=(",", ":")).encode(),
    )


def _is_url(value: str, *, query: bool) -> bool:
    """Return whether `value` is an `https` URL a client could use and quote."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.netloc)
        and "#" not in value
        and (query or "?" not in value)
        and _HEADER_SAFE.fullmatch(value) is not None
    )


def _holds_braces(url: str) -> bool:
    """Return whether a URL's path holds a brace, written as is or encoded."""
    return not {"{", "}"}.isdisjoint(unquote(urlsplit(url).path))


def _names(value: tuple[str, ...] | list[str], *, name: str) -> tuple[str, ...]:
    """Return `value` as a tuple, refusing a bare string.

    Raises:
        TypeError: If `value` is a string, which would read as one name per
            character.
    """
    if isinstance(value, str):
        msg = (
            f"{name}= takes a sequence of names, not a single string. Write "
            f"it as a tuple."
        )
        raise TypeError(msg)
    return tuple(value)


METADATA_MARKER: Final = "__grelmicro_resource_metadata__"
"""Set on the handler grelmicro registers to serve the metadata itself."""


def refuse_routes_at_metadata(
    app: Any,  # noqa: ANN401
    metadata: _ResourceMetadata | None,
) -> None:
    """Refuse a route the app declares where the metadata is served.

    The middleware answers every request at that path, whatever its method,
    so a route declared there would never run. The routes are the ones the
    app's integration lists in `route_declarations(app)`, which leaves out
    a route grelmicro registers there itself. An integration that lists
    none is not checked.

    Raises:
        TypeError: Naming the path of the first such route.
    """
    if metadata is None:
        return
    for declaration in _listed(app):
        if declaration.path not in metadata.paths:
            continue
        msg = (
            f"{declaration.path} is where resource= publishes the protected "
            f"resource metadata, and AuthenticatedRequests answers every "
            f"request there, so the route declared at that path never runs. "
            f"Remove the route, or leave resource= unset."
        )
        raise TypeError(msg)


def _pointing_at(send: Send, pointer: bytes) -> Send:
    """Return `send`, adding `pointer` to every bearer challenge it sends."""

    async def sending(message: Message) -> None:
        if message["type"] in _RESPONSE_STARTS:
            message = {
                **message,
                "headers": [
                    (
                        name,
                        _pointed(value, pointer)
                        if name.lower() == b"www-authenticate"
                        else value,
                    )
                    for name, value in message.get("headers") or ()
                ],
            }
        await send(message)

    return sending


def _pointed(challenge: bytes, pointer: bytes) -> bytes:
    """Return a bearer challenge carrying `pointer`, or any other one unchanged.

    Only a lone bearer challenge whose parameters are all quoted is added
    to, so a challenge in another scheme, or one naming its own metadata,
    reaches the client as the app wrote it.
    """
    if (
        b"resource_metadata=" in challenge.lower()
        or _SINGLE_BEARER.fullmatch(challenge) is None
    ):
        return challenge
    return challenge + (b", " if b"=" in challenge else b" ") + pointer


async def _nothing(scope: Scope, receive: Receive, send: Send) -> None:
    """Stand in for the app, so the options can be checked without one."""
