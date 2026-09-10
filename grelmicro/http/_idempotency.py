"""Idempotent requests.

`IdempotencyMiddleware` replays a stored response when a request repeats its
idempotency key, and `IdempotentRequests` registers it through `uses=[...]`
so `micro.install(app)` adds it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Self,
    TypedDict,
    cast,
)

from pydantic import BaseModel, NonNegativeFloat, PositiveInt, StrictStr
from typing_extensions import Doc

from grelmicro._config import (
    Live,
    Reconfigurable,
    build_config,
    env_prefixes,
    resolve_config,
)
from grelmicro._guards import is_instance, type_name
from grelmicro._paths import (
    BARE_METHOD_MESSAGE,
    MethodNames,
    PathPatterns,
    _is_mount,
    _routing_app,
    _wrapped_app,
    as_patterns,
    route_path,
    selects,
    walk_routes,
)
from grelmicro.errors import OutOfContextError, SettingsValidationError
from grelmicro.http._component import ErrorResponses, send_error
from grelmicro.http._kinds import (
    _IN_FLIGHT_RETRY_AFTER,
    BODYLESS_STATUSES,
    IDEMPOTENCY_IN_FLIGHT,
    IDEMPOTENCY_KEY_INVALID,
    IDEMPOTENCY_KEY_REUSED,
    REQUEST_BODY_TOO_LARGE,
    Kind,
    Occurrence,
)
from grelmicro.idempotency import Idempotency
from grelmicro.idempotency.errors import (
    IdempotencyConflictError,
    IdempotencyKeyMakerError,
    IdempotencyWaitTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Collection,
        MutableMapping,
        Sequence,
    )
    from types import TracebackType

    from grelmicro.cache import TTLCache

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["IdempotencyMiddleware", "IdempotentRequests", "StoredResponse"]

_logger = logging.getLogger(__name__)


_KEY_PATTERN = r"^[\x20-\x7e]+$"
"""The key rule, as the OpenAPI schema publishes it."""

_KEY_CHARS = re.compile(_KEY_PATTERN)
"""What an idempotency key may hold: printable US-ASCII, and nothing else.

A control byte or a byte above `0x7e` reaches the cache as a key, travels
through proxies that may rewrite it, and reads back as mojibake. The schema
publishes this, so the wire enforces it.
"""


_MAX_KEY_LENGTH = 255
"""Longest accepted idempotency key, in characters.

A longer key is answered with `400`.
"""


_DEFAULT_REPLAY_HEADER = "Idempotent-Replayed"
"""Response header marking a replayed response.

No standard names one. The Idempotency-Key header draft registers the
request header alone, so this is the name most APIs answer a replay with.
`replay_header` takes another.
"""


_FIELD_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
"""What an HTTP field name may hold, as RFC 9110 spells a token.

A name holding anything else, a space, a colon, or a newline above all, is
refused at construction rather than reaching the wire as a broken header.
"""


_RESERVED_REPLAY_HEADERS = frozenset(
    {
        "age",
        "allow",
        "cache-control",
        "connection",
        "content-disposition",
        "content-encoding",
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "expires",
        "last-modified",
        "location",
        "retry-after",
        "set-cookie",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "vary",
        "www-authenticate",
    }
)
"""Names `replay_header` is refused.

Each one frames the response, labels it, or tells a cache what to do with
it, so the marker taking its place would break the exchange rather than
annotate it. `ETag: true` is the sharpest case: every replay would carry
one entity tag, and a later `If-None-Match` would match a resource the
client never held. Any other name the stored response carries is logged
when the marker replaces it.
"""


_REPLAY_VALUE = "true"
"""What the replay marker carries."""


_REPLAY_MARK = _REPLAY_VALUE.encode("ascii")
"""What the replay marker carries, as it goes on the wire."""


_REPLAYED_SCOPE_KEY = "grelmicro.idempotency.replayed"
"""Set on the scope by the middleware that answers a request with a replay.

A middleware of this kind running above that one reads it to tell a marker
of ours from one a handler wrote, which look the same on the wire.
"""


_MIN_CONTENT_STATUS = 200
"""Lowest status that may carry content."""


_KEY_SEPARATOR = "\x1f"
"""Separator joining the parts of a stored key."""


_DEFAULT_KEY_VERSION = "v2"
"""Version isolating safe default keys from entries written before this policy."""


_PRIVATE_REQUEST_HEADERS = frozenset({b"authorization", b"cookie"})
"""Headers proving that a response was computed for one caller."""


class _GatedRoutes:
    """Routes whose dependencies or authentication must run before replay."""

    __slots__ = ("_apps", "_authenticated", "_routes", "_wrapped")

    def __init__(self, app: Any = None) -> None:  # noqa: ANN401
        """Remember the wrapped app and defer route discovery to a request."""
        self._wrapped = app
        self._apps: tuple[Any, ...] = ()
        self._authenticated: tuple[re.Pattern[str], ...] = ()
        self._routes: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = ()

    def read(self, *apps: Any) -> None:  # noqa: ANN401
        """Read dependency-bearing and authenticated routes from the apps."""
        self._apps = apps
        roots: list[Any] = []
        for app in apps:
            root = _routing_app(app)
            if root is not None and not any(root is seen for seen in roots):
                roots.append(root)
        authenticated = {
            path for app in apps for path in _authentication_paths(app)
        }
        fastapi_roots = [root for root in roots if _contains_fastapi(root)]
        if not authenticated and not fastapi_roots:
            self._authenticated = ()
            self._routes = ()
            return
        from starlette.routing import compile_path  # noqa: PLC0415

        protected: list[re.Pattern[str]] = []
        for prefix, nested in sorted(authenticated):
            exact, _, _ = compile_path(prefix or "/")
            protected.append(exact)
            if nested:
                descendant, _, _ = compile_path(
                    f"{prefix.rstrip('/')}/{{path:path}}"
                    if prefix
                    else "/{path:path}"
                )
                protected.append(descendant)
        self._authenticated = tuple(protected)

        found: list[tuple[re.Pattern[str], frozenset[str]]] = []
        for root in fastapi_roots:
            for prefix, route, contexts in walk_routes(
                root, unwrap_middleware=True
            ):
                if not _has_dependencies(route, contexts):
                    continue
                compiled, _, _ = compile_path(f"{prefix}{route.path}")
                methods = frozenset(
                    method.upper()
                    for method in (getattr(route, "methods", None) or ())
                )
                found.append((compiled, methods))
        self._routes = tuple(found)

    def matches(self, scope: Scope) -> bool:
        """Return whether authentication or a dependency guards the route."""
        apps = _unique_apps((self._wrapped, scope.get("app")))
        if len(apps) != len(self._apps) or any(
            app is not seen for app, seen in zip(apps, self._apps, strict=True)
        ):
            self.read(*apps)
        method = scope["method"]
        path = route_path(scope)
        return any(
            regex.fullmatch(path) for regex in self._authenticated
        ) or any(
            method in methods and regex.fullmatch(path)
            for regex, methods in self._routes
        )


def _unique_apps(apps: tuple[Any, ...]) -> tuple[Any, ...]:
    """Return non-null applications once each, preserving identity order."""
    found: list[Any] = []
    for app in apps:
        if app is not None and not any(app is seen for seen in found):
            found.append(app)
    return tuple(found)


def _contains_fastapi(app: Any) -> bool:  # noqa: ANN401
    """Return whether an application is FastAPI or mounts one."""
    pending = [app]
    seen: set[int] = set()
    while pending:
        current = _routing_app(pending.pop())
        if current is None:
            continue
        if id(current) in seen:
            continue
        seen.add(id(current))
        if any(
            klass.__module__.partition(".")[0] == "fastapi"
            for klass in type(current).__mro__
        ):
            return True
        if _is_mount(current):
            pending.append(getattr(current, "app", None))
        router = getattr(current, "router", None)
        for route in getattr(router or current, "routes", ()) or ():
            nested = getattr(route, "app", None)
            if nested is not None:
                pending.append(nested)
    return False


def _authenticated_scope(scope: Scope) -> bool:
    """Return whether authentication already established a caller identity."""
    user = scope.get("user")
    if user is not None and getattr(user, "is_authenticated", False):
        return True
    auth = scope.get("auth")
    return bool(getattr(auth, "scopes", ()))


def _is_authentication_middleware(value: Any) -> bool:  # noqa: ANN401
    """Return whether a class or instance is Starlette authentication."""
    classes = value.__mro__ if isinstance(value, type) else type(value).__mro__
    return any(
        klass.__module__ == "starlette.middleware.authentication"
        and klass.__name__ == "AuthenticationMiddleware"
        for klass in classes
    )


def _authentication_here(app: Any) -> bool:  # noqa: ANN401
    """Return whether this application boundary authenticates requests."""
    if _authentication_chain(app):
        return True
    routed = _routing_app(app)
    if routed is None:
        return False
    if any(
        _is_authentication_middleware(getattr(middleware, "cls", middleware))
        for middleware in getattr(routed, "user_middleware", ())
    ):
        return True
    return _authentication_chain(getattr(routed, "middleware_stack", None))


def _authentication_chain(app: Any) -> bool:  # noqa: ANN401
    """Return whether an instantiated ASGI chain contains authentication."""
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        if _is_authentication_middleware(app):
            return True
        app = _wrapped_app(app)
    return False


def _authentication_paths(app: Any) -> set[tuple[str, bool]]:  # noqa: ANN401
    """Return exact or nested paths protected by Starlette authentication."""
    found: set[tuple[str, bool]] = set()

    def visit(current: Any, prefix: str, ancestors: frozenset[int]) -> None:  # noqa: ANN401
        routed = _routing_app(current)
        if routed is None or id(routed) in ancestors:
            return
        if _authentication_here(current):
            found.add((prefix, True))
            return
        nested_ancestors = ancestors | {id(routed)}
        if _is_mount(routed):
            path = f"{prefix}{getattr(routed, 'path', '')}"
            nested = getattr(routed, "app", None)
            if _authentication_here(nested):
                found.add((path, True))
            else:
                visit(nested, path, nested_ancestors)
            return
        router = getattr(routed, "router", None)
        for route in getattr(router or routed, "routes", ()) or ():
            included = getattr(route, "original_router", None)
            if included is not None:
                context = getattr(route, "include_context", None)
                visit(
                    included,
                    f"{prefix}{getattr(context, 'prefix', '')}",
                    nested_ancestors,
                )
                continue
            path = f"{prefix}{getattr(route, 'path', '')}"
            nested = getattr(route, "app", None)
            if _authentication_here(nested):
                found.add((path, getattr(route, "routes", None) is not None))
                continue
            if getattr(route, "routes", None) is not None:
                visit(nested, path, nested_ancestors)

    visit(app, "", frozenset())
    return found


def _has_dependencies(route: Any, contexts: tuple[Any, ...]) -> bool:  # noqa: ANN401
    """Return whether FastAPI runs dependencies before this route."""
    dependency_tree = getattr(route, "dependant", None)  # codespell:ignore
    if getattr(dependency_tree, "dependencies", ()):
        return True
    return any(
        getattr(context, "dependencies", ()) or () for context in contexts
    )


def _field_name(value: str, argument: str, example: str) -> str:
    """Return `value`, or raise when it is not an HTTP field name.

    Only the shape is checked here. Whether it is a string at all is
    settled by the config's `StrictStr`, which refuses `bytes` rather
    than decoding them, so this is only ever handed one.

    Raises:
        SettingsValidationError: If it is not an HTTP field name.
    """
    if not _FIELD_NAME.fullmatch(value):
        msg = (
            f"{argument} is not an HTTP field name. Use letters, digits, "
            f"and the punctuation a header name takes, such as "
            f"{example!r}."
        )
        raise SettingsValidationError(msg)
    return value


_RESERVED_KEY_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "origin",
        "referer",
        "transfer-encoding",
        "user-agent",
    }
)
"""Names `key_header` is refused.

Every request carries one of these, so keying on it would merge callers
that share a value rather than a key. `Content-Type` is the sharpest
case: every JSON POST to one route would read as the same key, and one
caller's stored response would replay to the next.
"""


def _key_name(value: str) -> str:
    """Return `value`, or raise when it cannot carry an idempotency key."""
    _field_name(value, "key_header", "Idempotency-Key")
    if value.lower() in _RESERVED_KEY_HEADERS:
        msg = (
            "key_header cannot name a header every request already "
            "carries, such as Content-Type or Authorization. Callers "
            "sharing that value would share one stored response. Pick a "
            "header of your own, such as 'Idempotency-Key'."
        )
        raise SettingsValidationError(msg)
    return value


def _replay_name(value: str) -> str:
    """Return `value`, or raise when it cannot carry the replay marker."""
    _field_name(value, "replay_header", _DEFAULT_REPLAY_HEADER)
    if value.lower() in _RESERVED_REPLAY_HEADERS:
        msg = (
            "replay_header cannot name a header that directs the client, "
            "such as Content-Type, Location, or Content-Length. The marker "
            "would take its place. Pick a name of your own, such as "
            "'Idempotent-Replayed'."
        )
        raise SettingsValidationError(msg)
    return value


class StoredResponse(TypedDict):
    """The response `IdempotencyMiddleware` is about to store.

    Handed to `skip` so a handler's own rule decides whether a response
    replays. `headers` maps lowercased names to their value, keeping the
    last of a repeated name.
    """

    status: int
    headers: dict[str, str]
    body: bytes


class _Entry(TypedDict):
    """A stored response as it rides the cache.

    Header values and the body are `latin-1` strings, which round-trip
    any byte sequence through the cache serializers without loss.
    """

    status: int
    headers: Sequence[Sequence[str]]
    body: str


class IdempotentRequestsConfig(BaseModel, frozen=True, extra="forbid"):
    """Idempotent Requests Config.

    The window a key replays for lives on the `Idempotency` this rides,
    because that is the object that stores the response. It is named
    after the namespace, so the address is
    `GREL_IDEMPOTENCY_{NAMESPACE}_TTL`, `GREL_IDEMPOTENCY_HTTP_TTL` by
    default.
    """

    key_header: Annotated[
        StrictStr,
        Doc("Request header carrying the idempotency key."),
    ] = "Idempotency-Key"
    replay_header: Annotated[
        StrictStr,
        Doc("Response header marking a replayed response."),
    ] = _DEFAULT_REPLAY_HEADER
    methods: Annotated[
        MethodNames,
        Doc(
            "Methods that take an idempotency key. Every other method "
            "passes through."
        ),
    ] = ("POST",)
    require_key: Annotated[
        bool,
        Doc(
            "Answer `400` when a method in `methods` arrives without the "
            "header, instead of passing it through."
        ),
    ] = False
    fingerprint_body: Annotated[
        bool,
        Doc(
            "Hash the request body and store the hash with the response, "
            "so a key reused with a different body is refused."
        ),
    ] = False
    max_body_size: Annotated[
        PositiveInt,
        Doc("Largest body held in memory, in bytes."),
    ] = 1024 * 1024
    wait_timeout: Annotated[
        NonNegativeFloat,
        Doc(
            "Seconds a duplicate waits for an execution already in "
            "flight, before it is answered with `409`."
        ),
    ] = 10.0
    include: Annotated[
        PathPatterns,
        Doc("Paths this middleware acts on. Empty means every path."),
    ] = ()
    exclude: Annotated[
        PathPatterns,
        Doc("Paths this middleware leaves alone, whatever `include` says."),
    ] = ()
    reused_status: Annotated[
        int,
        Doc("Status answering a key reused with a different payload."),
    ] = IDEMPOTENCY_KEY_REUSED.status


@dataclass(frozen=True, slots=True)
class _State:
    """What the middleware answers one request from.

    Holds the configuration beside the values derived from it, so a
    reader takes both in one read. The header names are folded to the
    lower-case bytes a scope carries, and the methods to the upper case,
    because a request is matched against them and a caller may have
    written either.
    """

    config: IdempotentRequestsConfig
    methods: frozenset[str]
    header: bytes
    replay_header: bytes
    reused: Kind


def _state_of(config: IdempotentRequestsConfig) -> _State:
    """Derive what the request path needs from a configuration."""
    return _State(
        config=config,
        methods=frozenset(method.upper() for method in config.methods),
        header=_key_name(config.key_header).lower().encode("ascii"),
        replay_header=(
            _replay_name(config.replay_header).lower().encode("ascii")
        ),
        reused=(
            IDEMPOTENCY_KEY_REUSED
            if config.reused_status == IDEMPOTENCY_KEY_REUSED.status
            else replace(IDEMPOTENCY_KEY_REUSED, status=config.reused_status)
        ),
    )


class IdempotencyMiddleware:
    """Replay a stored HTTP response when a request repeats its idempotency key.

    A request whose method is listed in `methods` and which carries the
    `key_header` runs once. A retry with the same key replays the stored
    status, headers, and body without reaching the handler, and carries
    the `replay_header` marker, `Idempotent-Replayed: true` by default. A
    request without the `key_header` passes straight through, so adding
    the middleware changes nothing until a client opts in.

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.http import IdempotencyMiddleware
    from grelmicro.cache import TTLCache
    from grelmicro.idempotency import Idempotency

    micro = Grelmicro(uses=[...])
    app = FastAPI()
    micro.install(app)

    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=3600)
    )
    ```

    Register `IdempotentRequests()` instead to have `micro.install(app)`
    add it for you, along with the OpenAPI documentation.

    Added by hand, it goes before or after `micro.install(app)`. It
    resolves its `Cache` through the grelmicro request scope, which
    `install` keeps outside every other middleware.

    A duplicate that arrives while the first execution is in flight waits
    for it and replays its response. The wait folds across replicas when
    a `Coordination` lock backend is configured, and in-process
    otherwise. It is bounded by `wait_timeout`.

    Every response the app returns is stored, errors included. A handler
    that raises an unhandled exception stores nothing, so the framework's
    `500` never replays.

    Without a custom `key_maker`, a request carrying `Authorization` or
    `Cookie` bypasses idempotency and runs the app. Its response cannot be
    stored under a key shared across callers, and a replay cannot skip
    authentication inside the app. Configure an identity-aware `key_maker`
    to make authenticated requests idempotent.

    Four kinds of response are not stored, and each one lets a retry re-run
    the handler: one carrying `Set-Cookie`, one carrying `Content-Encoding`,
    one declaring trailers, and one whose body is over `max_body_size`. All
    four are logged. Pass `skip` to add a rule of your own.

    Background tasks run after the response is sent, so a replay can be
    served while the original request's background work is still in
    flight.

    The middleware is pure ASGI and works with any ASGI framework
    (Starlette, Litestar, ...). It acts on `http` scopes and passes every
    other scope through untouched.
    """

    def __init__(  # noqa: PLR0913
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        idempotency: Annotated[
            Idempotency[Any],
            Doc(
                "The `Idempotency` that stores responses. Its `ttl` sets "
                "how long a key replays."
            ),
        ],
        key_header: Annotated[
            str,
            Doc("Request header carrying the idempotency key."),
        ] = "Idempotency-Key",
        replay_header: Annotated[
            str,
            Doc(
                """
                Response header marking a replayed response.

                No standard names one, so pick what the clients already
                read. `Idempotent-Replayed` is what most APIs answer with.
                """
            ),
        ] = _DEFAULT_REPLAY_HEADER,
        methods: Annotated[
            Collection[str],
            Doc(
                "Methods that take an idempotency key. Every other method "
                "passes through."
            ),
        ] = ("POST",),
        key_maker: Annotated[
            Callable[[Scope, str], str] | None,
            Doc(
                """
                Build the stored key from the ASGI scope and the client key.

                Defaults to the method, the path, the query string, and
                the client key, so two public routes never replay each
                other. Requests carrying `Authorization` or `Cookie`
                bypass that unscoped default. Set this to an identity-aware
                key in a multi-tenant app that needs authenticated replay.
                """
            ),
        ] = None,
        skip: Annotated[
            Callable[[StoredResponse], bool] | None,
            Doc(
                """
                Predicate receiving the response. Return `True` to not store it.

                Mirrors `skip` on `@cached`. Use it for a response that
                is technically replayable but should not be, such as one
                whose body embeds a timestamp the caller must not see
                twice. Responses that are never safe to replay are
                dropped before this runs.
                """
            ),
        ] = None,
        require_key: Annotated[
            bool,
            Doc(
                "Answer `400` when a method in `methods` arrives without "
                "the header, instead of passing it through."
            ),
        ] = False,
        fingerprint_body: Annotated[
            bool,
            Doc(
                """
                Hash the request body and store the hash with the response.

                A key reused with a different body then gets `422` instead
                of a wrong replay. Buffers the request body before the
                handler runs, and answers `413` when it is over
                `max_body_size`.
                """
            ),
        ] = False,
        max_body_size: Annotated[
            int,
            Doc(
                "Largest body held in memory, in bytes. A larger response "
                "is sent to the client and not stored. With "
                "`fingerprint_body`, a larger request body is answered "
                "with `413`."
            ),
        ] = 1024 * 1024,
        wait_timeout: Annotated[
            float,
            Doc(
                """
                Seconds a duplicate waits for an execution already in flight.

                Past it the duplicate is answered with `409` and a
                `Retry-After` header.
                """
            ),
        ] = 10.0,
        include: Annotated[
            tuple[str, ...],
            Doc(
                "Paths this middleware acts on. Empty means every path. "
                "Exact match unless the pattern ends with `*`, which "
                "matches as a prefix, so a router mounted under "
                '`/payments` is `"/payments/*"`.'
            ),
        ] = (),
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths this middleware leaves alone, whatever `include` "
                "says. Same matching."
            ),
        ] = (),
        reused_status: Annotated[
            int,
            Doc(
                """
                Status answering a key reused with a different payload.

                `422` is what the Idempotency-Key header draft asks for.
                Pass `400` where the clients expect that instead. The body
                is the same either way, so a client reading the `type`
                identifier is unaffected.
                """
            ),
        ] = IDEMPOTENCY_KEY_REUSED.status,
        live: Annotated[
            Live[_State] | None,
            Doc(
                "The cell a registered `IdempotentRequests` publishes its "
                "snapshot into, filled by `micro.install(app)`. Passing it "
                "makes the other options the component's to decide."
            ),
        ] = None,
    ) -> None:
        """Initialize the middleware with the idempotency store and policy.

        Raises:
            TypeError: If `methods` is given as a string. `tuple("POST")`
                is four one-letter methods, none of which a request
                carries, so it would meter nothing at all.
        """
        if isinstance(methods, str):
            raise TypeError(BARE_METHOD_MESSAGE)
        self.app = app
        self._idempotency = idempotency
        self._key_maker = key_maker
        self._skip = skip
        self._gated_routes = _GatedRoutes(app)
        self._replay_collision_logged = False
        # A middleware built by hand owns its cell and never sees a new
        # snapshot, so the two doors read exactly the same way.
        self._live = (
            live
            if live is not None
            else Live(
                _state_of(
                    build_config(
                        IdempotentRequestsConfig,
                        key_header=key_header,
                        replay_header=replay_header,
                        methods=tuple(methods),
                        require_key=require_key,
                        fingerprint_body=fingerprint_body,
                        max_body_size=max_body_size,
                        wait_timeout=wait_timeout,
                        include=as_patterns(include, name="include"),
                        exclude=as_patterns(exclude, name="exclude"),
                        reused_status=reused_status,
                    )
                )
            )
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Replay, execute, or pass the request through."""
        # One read, at the top, for the whole request. A reconfigure
        # publishes a new snapshot between requests, and one already
        # running finishes on the one it started with.
        state = self._live.state
        config = state.config
        if (
            scope["type"] != "http"
            or scope["method"] not in state.methods
            or not selects(
                route_path(scope),
                include=config.include,
                exclude=config.exclude,
            )
        ):
            await self.app(scope, receive, send)
            return

        header_name = config.key_header
        key = _header_value(scope["headers"], state.header)
        if not key:
            if config.require_key:
                await _refuse(
                    send,
                    scope,
                    IDEMPOTENCY_KEY_INVALID,
                    f"The {header_name} header is required on this "
                    f"request and was not sent.",
                )
                return
            await self.app(scope, receive, send)
            return

        if len(key) > _MAX_KEY_LENGTH:
            await _refuse(
                send,
                scope,
                IDEMPOTENCY_KEY_INVALID,
                f"The {header_name} header is longer than "
                f"{_MAX_KEY_LENGTH} characters.",
            )
            return

        if not _KEY_CHARS.match(key):
            await _refuse(
                send,
                scope,
                IDEMPOTENCY_KEY_INVALID,
                f"The {header_name} header holds a character it "
                f"cannot carry. Use printable ASCII, such as a UUID.",
            )
            return

        if self._unscoped_private(scope):
            await self.app(scope, receive, send)
        else:
            await self._execute_key(scope, receive, send, state, key)

    def _unscoped_private(self, scope: Scope) -> bool:
        """Return whether the default key cannot safely replay this request."""
        return self._key_maker is None and (
            _has_private_request_header(scope["headers"])
            or _authenticated_scope(scope)
            or self._gated_routes.matches(scope)
        )

    async def _execute_key(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        state: _State,
        key: str,
    ) -> None:
        """Fingerprint the request when configured, then execute its key."""
        fingerprint = None
        if state.config.fingerprint_body:
            body, too_large, receive = await _buffer_request(
                receive, state.config.max_body_size
            )
            if too_large:
                await _refuse(send, scope, REQUEST_BODY_TOO_LARGE)
                return
            if body is not None:
                fingerprint = hashlib.sha256(body).hexdigest()

        await self._execute(
            scope,
            receive,
            send,
            state,
            self._storage_key(scope, key),
            fingerprint,
        )

    async def _execute(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        state: _State,
        storage_key: str,
        fingerprint: str | None,
    ) -> None:
        """Run the request under the idempotency block, or replay it.

        Takes the snapshot `__call__` read rather than reading its own, so
        one request answers from one configuration throughout.
        """
        config = state.config
        header_name = config.key_header
        block = self._idempotency(
            storage_key,
            fingerprint=fingerprint,
            wait_timeout=config.wait_timeout,
        )
        try:
            operation = await block.__aenter__()
        except IdempotencyConflictError:
            await _refuse(
                send,
                scope,
                state.reused,
                f"The {header_name} header was already used with a "
                f"different request payload. Use a fresh key, or resend the "
                f"original payload.",
            )
            return
        except IdempotencyWaitTimeoutError:
            await _refuse(
                send,
                scope,
                IDEMPOTENCY_IN_FLIGHT,
                f"A request with this {header_name} is still running. "
                f"Retry after the delay in the Retry-After header to read "
                f"its response.",
                retry_after=_IN_FLIGHT_RETRY_AFTER,
            )
            return
        except OutOfContextError as exc:
            raise OutOfContextError(_OUT_OF_CONTEXT_HINT) from exc

        try:
            if operation.replayed:
                # Read by a middleware of this kind running above this one,
                # which would otherwise take the marker for a handler's.
                scope[_REPLAYED_SCOPE_KEY] = True
                await _send_stored(
                    send,
                    operation.result(),
                    head=scope["method"] == "HEAD",
                    replay_header=state.replay_header,
                )
            else:
                capture = _ResponseCapture(
                    send,
                    config.max_body_size,
                    self._skip,
                    state.replay_header,
                    scope,
                )
                await self.app(scope, receive, capture)
                self._report_collision(
                    config.replay_header, replaced=capture.replaced
                )
                if capture.stored is not None:
                    operation.store(capture.stored)
        except BaseException as exc:
            await block.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await block.__aexit__(None, None, None)

    def _report_collision(self, name: str, *, replaced: bool) -> None:
        """Say once that a response carried the marker's name itself.

        A response is captured without the marker, so this is a value of
        the app's own making way, whether it came from a handler or from a
        middleware of this kind running under this one.
        """
        if not replaced or self._replay_collision_logged:
            return
        self._replay_collision_logged = True
        _logger.warning(
            "The %s header a response carried made way for the replay "
            "marker. Give replay_header a name of its own where that "
            "value matters.",
            name,
        )

    def _storage_key(self, scope: Scope, key: str) -> str:
        """Build the stored key, scoped by route unless `key_maker` says otherwise."""
        if self._key_maker is not None:
            return _checked_key(self._key_maker(scope, key), key)
        # The whole path, where `include` and `exclude` read the route: two
        # apps mounted side by side declare the same routes, and a key
        # without the prefix would have them replay each other.
        parts = [_DEFAULT_KEY_VERSION, scope["method"], scope["path"]]
        query = scope.get("query_string", b"")
        if query:
            parts.append(query.decode("latin-1"))
        parts.append(key)
        return _KEY_SEPARATOR.join(parts)


_UNRESOLVED_TOKEN = re.compile(r"(?:^|[^0-9A-Za-z_])None(?:$|[^0-9A-Za-z_])")
"""A formatted `None` sitting on its own between separators in a key."""


def _checked_key(built: object, client_key: str) -> str:
    """Return `built`, or raise when it cannot separate one caller from another.

    A key that is partly missing does not fail, it merges. Every caller whose
    key lost the same component lands in one entry, and the request still
    answers normally, so the widening is invisible. That is a confidentiality
    boundary quietly removed, which is worth refusing over.

    Raises:
        IdempotencyKeyMakerError: If the key is not a non-empty string, drops
            the client's key, or carries an unresolved `None`.
    """
    if not is_instance(built, str):
        # Named by its type, never printed: what a `key_maker` returns is
        # built from caller data, and reading its `__repr__` runs caller
        # code that a detached object raises from.
        msg = (
            f"key_maker returned a {type_name(built)}, expected a "
            f"non-empty string."
        )
        raise IdempotencyKeyMakerError(msg)
    # An exact `str`, whatever subclass it arrived as: a subclass runs
    # caller code again from `__str__` the moment it is interpolated.
    built = str.__str__(cast("str", built))
    if not built:
        msg = "key_maker returned an empty string, expected a non-empty key."
        raise IdempotencyKeyMakerError(msg)
    if client_key not in built:
        msg = (
            f"key_maker returned {built!r}, which drops the client's "
            f"idempotency key. Every request to this route would then share "
            f"one entry. Include the key it was given."
        )
        raise IdempotencyKeyMakerError(msg)
    if _UNRESOLVED_TOKEN.search(built):
        msg = (
            f"key_maker returned {built!r}, which carries an unresolved None. "
            f"Something the key reads was not set yet, so that component is "
            f"the same for every caller and they share one entry. A middleware "
            f"the key depends on must run outside IdempotencyMiddleware, which "
            f"means adding it after."
        )
        raise IdempotencyKeyMakerError(msg)
    return built


_OUT_OF_CONTEXT_HINT = (
    "IdempotencyMiddleware resolved no cache backend. Call micro.install(app) "
    "so the grelmicro request scope wraps it, register a Cache component, or "
    "pass an explicit cache= to Idempotency."
)


class _ResponseCapture:
    """Forward an ASGI response downstream while copying it for storage.

    Each chunk reaches the client as the handler produces it, so storing
    a response adds no latency. `stored` stays None until the final body
    message arrives, so a response torn off midway is never replayed.
    """

    def __init__(
        self,
        send: Send,
        max_body_size: int,
        skip: Callable[[StoredResponse], bool] | None = None,
        replay_header: bytes = b"",
        scope: Scope | None = None,
    ) -> None:
        """Initialize the capture around the downstream `send`."""
        self._send = send
        self._max_body_size = max_body_size
        self._skip = skip
        self._replay_header = replay_header
        self._scope = scope if scope is not None else {}
        self.replaced = False
        self._status = 0
        self._headers: list[tuple[str, str]] = []
        self._chunks: list[bytes] = []
        self._size = 0
        self._storable = False
        self.stored: _Entry | None = None

    async def __call__(self, message: Message) -> None:
        """Capture the message, then forward it downstream."""
        if message["type"] == "http.response.start":
            self._start(message)
        elif message["type"] == "http.response.body":
            self._body(message)
        await self._send(message)

    def _start(self, message: Message) -> None:
        """Record the status and headers, and decide whether to store."""
        blockers: list[str] = []
        replayed_below = self._scope.get(_REPLAYED_SCOPE_KEY, False)
        for name, _value in message["headers"]:
            lowered = name.lower()
            if lowered == b"set-cookie":
                blockers.append("Set-Cookie")
            elif lowered == b"content-encoding":
                blockers.append("Content-Encoding")
            elif lowered == self._replay_header and not replayed_below:
                # The marker says a response is a replay, and this one is
                # not: a middleware of this kind under this one would have
                # said so on the scope. A handler writing the name would
                # have every fresh response read as a replay, so its value
                # makes way.
                self.replaced = True
        if self.replaced:
            message["headers"] = [
                (name, value)
                for name, value in message["headers"]
                if name.lower() != self._replay_header
            ]
        if message.get("trailers"):
            blockers.append("trailers")
        self._status = message["status"]
        # Content-Length is recomputed on replay, so a stored value that
        # drifts from the stored body can never reach a client.
        # The stored copy carries no marker at all, whoever set it: a
        # replay of this response adds one, and two would say it twice.
        self._headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in message["headers"]
            if name.lower() not in (b"content-length", self._replay_header)
        ]
        self._storable = not blockers
        if blockers:
            _logger.warning(
                "Idempotent response not stored: it carries %s. A retry with "
                "the same key will run the handler again.",
                " and ".join(blockers),
            )

    def _body(self, message: Message) -> None:
        """Accumulate the body, or give up once it outgrows the limit."""
        if not self._storable:
            return
        chunk = message.get("body", b"")
        self._size += len(chunk)
        if self._size > self._max_body_size:
            self._storable = False
            self._chunks.clear()
            _logger.warning(
                "Idempotent response not stored: body exceeds max_body_size "
                "(%d bytes). A retry with the same key will run the handler "
                "again.",
                self._max_body_size,
            )
            return
        self._chunks.append(chunk)
        if message.get("more_body", False):
            return
        body = b"".join(self._chunks)
        if self._skip is not None and self._skip(
            StoredResponse(
                status=self._status,
                headers=dict(self._headers),
                body=body,
            )
        ):
            return
        self.stored = _Entry(
            status=self._status,
            headers=self._headers,
            body=body.decode("latin-1"),
        )


def _header_value(
    headers: Sequence[tuple[bytes, bytes]], name: bytes
) -> str | None:
    """Return the first value of `name`, or None when it is absent."""
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1").strip()
    return None


def _has_private_request_header(
    headers: Sequence[tuple[bytes, bytes]],
) -> bool:
    """Return whether the request carries credentials for one caller."""
    return any(
        name.lower() in _PRIVATE_REQUEST_HEADERS for name, _value in headers
    )


async def _buffer_request(
    receive: Receive, max_body_size: int
) -> tuple[bytes | None, bool, Receive]:
    """Read the request body, and return it with a receive that replays it.

    Returns `(body, too_large, receive)`. `body` is None when the client
    disconnected before the last chunk, so the caller fingerprints
    nothing rather than hashing a truncated payload as if it were whole.
    `too_large` reports a body over `max_body_size`, which the caller
    answers with `413` instead of buffering without bound.

    The returned receive replays the consumed messages one for one,
    trailing disconnect included, so the app downstream reads exactly
    what the client sent.
    """
    consumed: list[Message] = []
    chunks: list[bytes] = []
    size = 0
    complete = False
    while True:
        message = await receive()
        consumed.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > max_body_size:
            return None, True, receive
        chunks.append(chunk)
        if not message.get("more_body", False):
            complete = True
            break
    pending = iter(consumed)

    async def replay_receive() -> Message:
        message = next(pending, None)
        if message is None:
            return await receive()
        return message

    return (b"".join(chunks) if complete else None), False, replay_receive


async def _send_stored(
    send: Send, stored: _Entry, *, head: bool, replay_header: bytes
) -> None:
    """Send a stored response, marked as a replay.

    Content-Length is recomputed from the stored body, and left off the
    statuses that carry no content, so a replay stays a valid response.
    Nothing here carries the replay name: a response is captured without
    it, so the marker is the only value under it.
    """
    body = stored["body"].encode("latin-1")
    status = stored["status"]
    headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in stored["headers"]
    ]
    if status >= _MIN_CONTENT_STATUS and status not in BODYLESS_STATUSES:
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
    headers.append((replay_header, _REPLAY_MARK))
    await send(
        {
            "type": "http.response.start",
            "status": stored["status"],
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": b"" if head else body})


async def _refuse(
    send: Send,
    scope: Scope,
    kind: Kind,
    detail: str | None = None,
    *,
    retry_after: float | None = None,
) -> None:
    """Answer a request the middleware refuses itself, before the app runs.

    A middleware sits outside the routing layer, so no exception handler
    sees what it decides. It renders through whichever `ErrorResponses` the
    app registered, read from the app the ASGI scope carries, so what the
    schema publishes for these responses is what the wire returns. An app
    that registered none gets RFC 9457, which an error body always needs.
    """
    errors = _registered_errors(scope)
    rendered = errors._render_occurrence(  # noqa: SLF001
        Occurrence(
            kind,
            detail=detail,
            extensions=(
                {} if retry_after is None else {"retry_after": retry_after}
            ),
        ),
        instance=scope["path"],
    )
    await send_error(send, rendered)


def _registered_errors(scope: Scope) -> ErrorResponses:
    """Return the app's `ErrorResponses`, or the default when none is set."""
    app = scope.get("app")
    registered = getattr(
        getattr(app, "state", None), "grelmicro_error_responses", None
    )
    return registered if registered is not None else ErrorResponses()


class IdempotentRequests(Reconfigurable[IdempotentRequestsConfig]):
    """Replay repeated requests, wired by `micro.install(app)`.

    Register it and `install` adds `IdempotencyMiddleware` to the app and
    describes it in the OpenAPI schema, so the container holds the whole
    wiring:

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.http import ErrorResponses, IdempotentRequests
    from grelmicro.providers.redis import RedisProvider

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[Cache(redis), ErrorResponses(), IdempotentRequests()])
    app = FastAPI()
    micro.install(app)
    ```

    The bare form stores responses under `Idempotency("http")`, which keeps
    them for a day and rides the registered `Cache`. Pass an `Idempotency`
    of your own to set the lifetime, the namespace, or the cache it uses.

    Every option of `IdempotencyMiddleware` is taken here and forwarded,
    so a registered component and a hand-added middleware answer the same.

    A framework that serves no HTTP, such as FastStream, ignores it.

    Read more in the [Idempotency Middleware](../http/idempotency.md)
    docs.
    """

    kind: ClassVar[str] = "idempotent_requests"

    _IMMUTABLE_RECONFIGURE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        IdempotentRequestsConfig.model_fields
    )
    """Every field, because every one of them protects a repeat.

    Live reload tunes what a request costs, never what it is protected
    by. Take a path out of the reach and the next retry runs the
    operation a second time. Turn `fingerprint_body` off and a key reused
    with a different payload replays the first response instead of being
    refused. Lower `max_body_size` and a large response stops being
    stored at all. Each of those is the outcome idempotency exists to
    prevent, and none of them announces itself, so this component is
    configured at startup and changed by a deploy, where it is reviewed.

    The header names, `require_key` and `reused_status` are fixed for a
    second reason: the OpenAPI schema states them, and the schema is
    built once from the app as installed.

    Read off the config rather than listed, so a field added later is
    covered by this decision instead of becoming live by omission.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        ttl: Annotated[
            float | None,
            Doc(
                "Seconds a stored response replays for. Defaults to a "
                "day. Held by the `Idempotency` this rides, which is named "
                "after the namespace, so it is tuned live under "
                "`GREL_IDEMPOTENCY_HTTP_TTL`."
            ),
        ] = None,
        namespace: Annotated[
            str,
            Doc(
                "Namespace the stored keys sit under, so two sets of rules "
                "on one app never read each other's responses. Part of "
                "every stored key, so it is not live."
            ),
        ] = "http",
        cache: Annotated[
            TTLCache[Any] | None,
            Doc(
                "The `TTLCache` responses are stored in. Defaults to the "
                "registered `Cache` component."
            ),
        ] = None,
        key_header: Annotated[
            str | None,
            Doc("Request header carrying the idempotency key."),
        ] = None,
        replay_header: Annotated[
            str | None,
            Doc(
                "Response header marking a replayed response. No standard "
                "names one, so pick what the clients already read."
            ),
        ] = None,
        methods: Annotated[
            Collection[str] | None,
            Doc(
                "Methods that take an idempotency key. Every other method "
                "passes through."
            ),
        ] = None,
        key_maker: Annotated[
            Callable[[Scope, str], str] | None,
            Doc(
                "Build the stored key from the ASGI scope and the client "
                "key. **Set this in any multi-tenant app**, folding in the "
                "caller identity."
            ),
        ] = None,
        skip: Annotated[
            Callable[[StoredResponse], bool] | None,
            Doc(
                "Predicate receiving the response. Return `True` to not "
                "store it."
            ),
        ] = None,
        require_key: Annotated[
            bool | None,
            Doc(
                "Answer `400` when a method in `methods` arrives without "
                "the header, instead of passing it through."
            ),
        ] = None,
        fingerprint_body: Annotated[
            bool | None,
            Doc(
                "Hash the request body and store the hash with the "
                "response, so a key reused with a different body gets "
                "`422` instead of a wrong replay."
            ),
        ] = None,
        max_body_size: Annotated[
            int | None,
            Doc("Largest body held in memory, in bytes."),
        ] = None,
        wait_timeout: Annotated[
            float | None,
            Doc(
                "Seconds a duplicate waits for an execution already in "
                "flight, before it is answered with `409`."
            ),
        ] = None,
        include: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Paths this middleware acts on. Empty means every path. "
                "Name the prefix of a router to select it, as "
                '`"/payments/*"`.'
            ),
        ] = None,
        exclude: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Paths this middleware leaves alone, whatever `include` "
                "says. Same matching."
            ),
        ] = None,
        reused_status: Annotated[
            int | None,
            Doc(
                "Status answering a key reused with a different payload. "
                "`422` is what the Idempotency-Key header draft asks for, "
                "and `400` is what some APIs answer instead."
            ),
        ] = None,
        openapi: Annotated[
            bool,
            Doc(
                "Describe both headers and the responses the middleware "
                "returns in the OpenAPI schema. Only FastAPI builds one. "
                "Read once when the schema is built, so it is not live."
            ),
        ] = True,
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
        env_prefix: Annotated[
            str | None,
            Doc(
                "Override the derived prefix, `GREL_IDEMPOTENT_REQUESTS_` "
                "for the default instance."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the "
                "process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> None:
        """Replay repeated requests through the registered cache."""
        resolved_env_prefix, kind_prefix = env_prefixes(
            "IDEMPOTENT_REQUESTS", name, env_prefix
        )
        config = resolve_config(
            IdempotentRequestsConfig,
            explicit=None,
            kwargs={
                "key_header": key_header,
                "replay_header": replay_header,
                "methods": methods,
                "require_key": require_key,
                "fingerprint_body": fingerprint_body,
                "max_body_size": max_body_size,
                "wait_timeout": wait_timeout,
                "include": include,
                "exclude": exclude,
                "reused_status": reused_status,
            },
            env_prefix=resolved_env_prefix,
            kind_env_prefix=kind_prefix,
            env_load=env_load,
        )
        self._setup(
            config,
            name=name,
            openapi=openapi,
            idempotency=Idempotency(namespace, ttl=ttl, cache=cache),
            key_maker=key_maker,
            skip=skip,
        )
        self._track_reconfigure(resolved_env_prefix)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            IdempotentRequestsConfig,
            Doc("The pre-built idempotent requests configuration."),
        ],
        *,
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
        namespace: Annotated[
            str,
            Doc("Namespace the stored keys sit under."),
        ] = "http",
        ttl: Annotated[
            float | None,
            Doc("Seconds a stored response replays for."),
        ] = None,
        cache: Annotated[
            TTLCache[Any] | None,
            Doc("The `TTLCache` responses are stored in."),
        ] = None,
        key_maker: Annotated[
            Callable[[Scope, str], str] | None,
            Doc("Build the stored key from the scope and the client key."),
        ] = None,
        skip: Annotated[
            Callable[[StoredResponse], bool] | None,
            Doc("Return `True` to leave one response unstored."),
        ] = None,
        openapi: Annotated[
            bool,
            Doc("Describe the headers in the OpenAPI schema."),
        ] = True,
    ) -> IdempotentRequests:
        """Build the component from a configuration that is already whole.

        The one declarative door. What you pass is what runs: no
        environment variable is read, and the instance is not registered
        for live reload. The store, the key maker and the skip predicate
        stay here rather than in the config, because they are objects and
        callables rather than values.
        """
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            config,
            name=name,
            openapi=openapi,
            idempotency=Idempotency(namespace, ttl=ttl, cache=cache),
            key_maker=key_maker,
            skip=skip,
        )
        return instance

    def _setup(
        self,
        config: IdempotentRequestsConfig,
        *,
        name: str,
        openapi: bool,
        idempotency: Idempotency[Any],
        key_maker: Callable[[Scope, str], str] | None,
        skip: Callable[[StoredResponse], bool] | None,
    ) -> None:
        """Hold the configuration and the cell the middleware reads."""
        self._name = name
        self._openapi = openapi
        self._idempotency = idempotency
        self._key_maker = key_maker
        self._skip = skip
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._live: Live[_State] = Live(_state_of(config))

    async def _apply_reconfigure(
        self, new_config: IdempotentRequestsConfig
    ) -> None:
        """Publish the snapshot the next request reads."""
        self._live.state = _state_of(new_config)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def idempotency(self) -> Idempotency[Any]:
        """Return the `Idempotency` the middleware stores through.

        For code that has to reach the store itself, such as clearing a key
        an operator asked about. Handlers need none of it: the middleware
        does the storing.
        """
        return self._idempotency

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with.

        `micro.install(app)` reads this from every registered component
        that carries it and hands the pair to the integration, which adds
        the middleware the way its framework takes one. A component
        without it wires no middleware.
        """
        return IdempotencyMiddleware, {
            "idempotency": self._idempotency,
            "key_maker": self._key_maker,
            "skip": self._skip,
            "live": self._live,
        }

    def handled_exceptions(self) -> tuple[type[Exception], ...]:
        """Return what this component answers rather than letting through.

        The middleware answers what it refuses itself. These are what the
        block form raises inside a handler, and registering the component
        is the opt-in for answering those the same way.
        """
        return (IdempotencyConflictError, IdempotencyWaitTimeoutError)

    def document_openapi(
        self,
        app: Annotated[Any, Doc("The FastAPI application to describe.")],  # noqa: ANN401
    ) -> None:
        """Describe the middleware in the app's OpenAPI schema.

        Called by the FastAPI integration after the middleware is added.
        A framework that builds no schema never calls it.
        """
        if not self._openapi:
            return
        from grelmicro.integrations.fastapi import (  # noqa: PLC0415
            document_idempotency,
        )

        document_idempotency(app, idempotency=self.idempotency)

    async def __aenter__(self) -> Self:
        """Open the component.

        Nothing to open. The wiring happens in `micro.install(app)`, which
        reads the registration and adds the middleware to the framework
        before it serves. This is the declaration that it should.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the component. Nothing to close."""
        return None
