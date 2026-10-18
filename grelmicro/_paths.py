"""Which paths a middleware acts on.

One matching rule for every grelmicro HTTP middleware, so a reader learns it
once: `include` narrows, `exclude` carves out, and `exclude` wins.
"""

from __future__ import annotations

import functools
import re
from ipaddress import IPv6Address, ip_address
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol

from pydantic import BeforeValidator
from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        MutableMapping,
        Sequence,
    )
    from re import Pattern

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

    class RouteReader(Protocol):
        """How an app names the route template of a request it answers."""

        def __call__(
            self,
            scope: Scope,
            root_path: str,
            path: str,
            status: int | None,
            /,
            *,
            reach: bool = False,
        ) -> str | None:
            """Return the route template of the request, `None` without one.

            Called with the scope, the root path and the path the request
            arrived with, and the status it was answered with, `None`
            before one started. A request no router reached names no
            route, unless `reach` is set: it then names the route its
            path would reach, matched against the app's routes.
            """
            ...


__all__ = [
    "ACCESS_RECORDED_KEY",
    "BARE_METHOD_MESSAGE",
    "BARE_NAME_MESSAGE",
    "BARE_STRING_MESSAGE",
    "MALFORMED_JSON_MESSAGE",
    "ROUTE_KEY",
    "ROUTE_PARAMETER",
    "ROUTE_READER_KEY",
    "FieldNames",
    "MethodNames",
    "PathPatterns",
    "RouteReading",
    "as_patterns",
    "holds_control_character",
    "litestar_mount",
    "litestar_owned_handler",
    "litestar_route_handler",
    "matches",
    "names_route",
    "path_format",
    "read_route",
    "refuse_bare_method",
    "refuse_bare_name",
    "refuse_bare_string",
    "route_path",
    "route_template",
    "segment_pattern",
    "selects",
    "starlette_route",
    "starlette_route_path",
]

_WHY_NOT_A_STRING = (
    "and this is a string. A string is a sequence of characters, so it "
    "would be walked one character at a time. Write it as a tuple with a "
    "trailing comma, or as a JSON list."
)
"""What a set of anything and a bare string have in common."""

BARE_STRING_MESSAGE = (
    f"a set of path patterns is expected, {_WHY_NOT_A_STRING} One "
    "pattern ending in `*` would otherwise match every path."
)
"""Why a bare string is refused where a set of patterns is expected.

Carries no example path. `SettingsValidationError` removes the rejected
value from the message it renders, so an example that happened to equal
what the caller passed would be taken out of the very sentence offering
it.
"""

BARE_METHOD_MESSAGE = f"a set of HTTP methods is expected, {_WHY_NOT_A_STRING}"
"""Why a bare string is refused where a set of methods is expected.

`methods="POST"` reads as `("P", "O", "S", "T")`, none of which is a
method, so the middleware would act on nothing at all.
"""

BARE_NAME_MESSAGE = f"a set of names is expected, {_WHY_NOT_A_STRING}"
"""Why a bare string is refused where a set of names is expected.

`vary_by_headers="accept-language"` reads one character at a time, and
nothing about a path describes it: there is no prefix to match and no
`*` to warn about.
"""


MALFORMED_JSON_MESSAGE = (
    "this looks like JSON and does not parse. A field holding many "
    "values is written as a JSON list, so check the brackets and the "
    "quotes."
)
"""Why a bracketed string was refused.

Told apart from a bare string on purpose. An operator who wrote
`["/livez"` did write a list, and answering that one is expected would
send them looking for the mistake they did not make.
"""

ROUTE_KEY: Final = "grelmicro.route"
"""Where the route a refused request names is left, `None` when no route answers it."""

ROUTE_READER_KEY: Final = "grelmicro.route_reader"
"""Where an installed app leaves its route reader, and how the request arrived.

Held as the reader, the root path and the path the request arrived with.
"""

ACCESS_RECORDED_KEY: Final = "grelmicro.access_recorded"
"""Where a request an access log records is marked, so a nested one writes nothing."""


class RouteReading:
    """Leave an app's route reader in the scope, with the request as it arrived.

    Wraps the whole app. The first one a request goes through leaves its
    reader, so inside a mount the outermost installed app's reader stays.
    `route_template` reads the route through it.
    """

    __slots__ = ("app", "reader")

    def __init__(self, app: ASGIApp, *, reader: RouteReader) -> None:
        """Wrap `app`, whose requests `reader` names the route of."""
        self.app = app
        self.reader = reader

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Leave the reader, unless an outer app left one, then run the app."""
        if scope["type"] != "lifespan" and ROUTE_READER_KEY not in scope:
            scope[ROUTE_READER_KEY] = (
                self.reader,
                scope.get("root_path", ""),
                scope["path"],
            )
        await self.app(scope, receive, send)


def _refuse(value: Any, message: str) -> Any:  # noqa: ANN401
    """Refuse a string where a set of them is expected.

    A string that opens a JSON list or object is reported as malformed
    JSON rather than as a missing comma, because that is what it is.

    Raises:
        ValueError: If the value is a string. A validator raises
            `ValueError` and never `TypeError`, because pydantic converts
            only the first into a validation error, so a `TypeError` here
            would escape `except SettingsValidationError` and the reload
            loop alike.
    """
    if isinstance(value, str):
        if value.strip().startswith(("[", "{")):
            raise ValueError(MALFORMED_JSON_MESSAGE)
        raise ValueError(message)  # noqa: TRY004
    return value


def refuse_bare_string(value: Any) -> Any:  # noqa: ANN401
    """Refuse a string where a set of path patterns is expected.

    Raises:
        ValueError: If the value is a string.
    """
    return _refuse(value, BARE_STRING_MESSAGE)


def refuse_bare_method(value: Any) -> Any:  # noqa: ANN401
    """Refuse a string where a set of HTTP methods is expected.

    Raises:
        ValueError: If the value is a string.
    """
    return _refuse(value, BARE_METHOD_MESSAGE)


def refuse_bare_name(value: Any) -> Any:  # noqa: ANN401
    """Refuse a string where a set of names is expected.

    Raises:
        ValueError: If the value is a string.
    """
    return _refuse(value, BARE_NAME_MESSAGE)


PathPatterns = Annotated[tuple[str, ...], BeforeValidator(refuse_bare_string)]
"""A set of path patterns on a config, with the bare string refused.

Every HTTP component declares its `include` and `exclude` with this, so
one missing comma is refused the same way wherever it is written.
"""

MethodNames = Annotated[tuple[str, ...], BeforeValidator(refuse_bare_method)]
"""A set of HTTP methods on a config, with the bare string refused.

The same mistake as `PathPatterns` refuses, said in the words of the
field it happened on, because `methods="POST"` is not a path.
"""

FieldNames = Annotated[tuple[str, ...], BeforeValidator(refuse_bare_name)]
"""A set of header or query names, with the bare string refused.

`vary_by_headers="accept-language"` is the same missing comma, and
nothing about a path pattern describes it: there is no prefix to match
and no `*` to warn about.
"""

_PREFIX = "*"
"""What turns a pattern into a prefix match, at the end of it."""

_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}
"""Ports omitted from a canonical authority for their scheme."""

_INVALID_PORT = object()
"""Marks an authority port that cannot be canonicalized safely."""

_MAX_PORT = 65535
"""Largest valid TCP port."""


ROUTE_PARAMETER = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([a-zA-Z_][a-zA-Z0-9_]*))?\}"
)
"""A path parameter in a route's path, with its optional converter's name."""


_PARAMETER = re.compile(r"\{[^{}:]+(:[^{}]+)?\}")
"""A parameter in a route template, with its converter if it names one."""


def segment_pattern(
    template: Annotated[
        str, Doc("A route's path, such as `/items/{item_id}`.")
    ],
) -> re.Pattern[str]:
    """Return the regex matching a request path one segment per parameter.

    A `path` parameter matches every remaining segment, and any other
    parameter one segment, whatever converter it names.
    """
    parts: list[str] = []
    last = 0
    for found in _PARAMETER.finditer(template):
        parts.append(re.escape(template[last : found.start()]))
        parts.append(".*" if found.group(1) == ":path" else "[^/]+")
        last = found.end()
    parts.append(re.escape(template[last:]))
    return re.compile(f"^{''.join(parts)}$")


def path_format(
    template: Annotated[
        str, Doc("A route's path, such as `/items/{item_id:int}`.")
    ],
) -> str:
    """Return the path with each parameter's converter left out.

    `/items/{item_id:int}` reads `/items/{item_id}`, as the schema names it.
    """
    return ROUTE_PARAMETER.sub(r"{\1}", template)


_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
"""A character no route is declared with, though a URL can decode to one."""


def holds_control_character(
    path: Annotated[str, Doc("The path the request is asking for.")],
) -> bool:
    """Return whether the path holds a control character, such as a newline.

    Starlette matches a route with `$`, which also matches before a final
    newline, so `/users/me` followed by a newline reaches the route declared
    `/users/me`. A check keyed on the declared path misses that spelling, so
    a guard that must not be bypassed treats such a path as its own case.
    """
    return _CONTROL_CHARACTER.search(path) is not None


def route_path(
    scope: Annotated[
        MutableMapping[str, Any], Doc("The ASGI scope of the request.")
    ],
) -> str:
    """Return the path the app's router matches the request with.

    `scope["path"]` carries the prefix a mount or a proxy adds, and
    `root_path` carries that prefix, so what is left is the path the route
    is written with. A pattern is therefore the same in an app served at
    the root, mounted under another app, or behind a proxy, and the same
    as the path the OpenAPI schema publishes.

    It is read exactly the way the framework's router reads it, because a
    check made against any other path checks a route other than the one
    that answers. Starlette takes `root_path` off whole and as given, so a
    `root_path` of `/api` leaves `/apikeys` alone and shortens `/api/keys`,
    and a path equal to it reads as empty. Litestar takes it off where it
    first appears, and normalizes the path whether it had one or not.

    The router is the one of the app in `scope["app"]`, which each framework
    sets to itself. Litestar's own key stays in the scope of an app mounted
    under it, so it names Litestar's router only when it is that same app.

    Middleware Litestar runs behind its router reads the path the router
    wrote back, which has the root path off already. A mount's is only what
    is left inside the mount, so the mount's own path is joined back to it.
    A handler the app does not own, such as the mount of an outer Litestar
    app, is read as no handler, and the path as before routing.

    Raises:
        RuntimeError: If Litestar routed the request to a mount its router
            does not list, so the path the mount serves is unknown.
    """
    path = _scope_text(scope["path"])
    root = _scope_text(scope.get("root_path", ""))
    litestar = _serving_litestar(scope)
    if litestar is None:
        return starlette_route_path(path, root)
    handler = litestar_owned_handler(litestar, scope.get("route_handler"))
    if handler is None:
        routed = path.split(root, maxsplit=1)[-1] if root else path
        return _litestar_normalize()(routed)
    if getattr(handler, "is_mount", False):
        return _litestar_mounted_path(litestar, handler, path)
    return path


def litestar_route_handler(
    scope: Annotated[
        MutableMapping[str, Any], Doc("The ASGI scope of the request.")
    ],
) -> Any | None:  # noqa: ANN401
    """Return the handler the router of the Litestar app serving the request matched.

    `None` when the app in `scope["app"]` is not a Litestar app, or when the
    handler in the scope is not one that app owns, such as the mount of an
    outer Litestar app before the app's own router matched.
    """
    litestar = _serving_litestar(scope)
    if litestar is None:
        return None
    return litestar_owned_handler(litestar, scope.get("route_handler"))


def _serving_litestar(scope: MutableMapping[str, Any]) -> Any | None:  # noqa: ANN401
    """Return the Litestar app in `scope["app"]`, or `None` for any other app.

    Litestar's own key stays in the scope of an app mounted under it, so it
    names the app serving the request only when it is that same app.
    """
    litestar = scope.get("litestar_app")
    if litestar is None or litestar is not scope.get("app"):
        return None
    return litestar


def litestar_owned_handler(app: Any, handler: Any) -> Any | None:  # noqa: ANN401
    """Return `handler` when the Litestar app `app` owns it, or `None`.

    Litestar gives each handler it registers the router it was registered
    on as its owner, and each router the one above it, up to the app.
    """
    if handler is None:
        return None
    owner = handler.owner
    while owner is not None:
        if owner is app:
            return handler
        owner = owner.owner
    return None


def _litestar_mounted_path(app: Any, handler: Any, remaining: str) -> str:  # noqa: ANN401
    """Return the path Litestar routed to a mount, the mount's own path included.

    Raises:
        RuntimeError: If the router lists no mount for the handler.
    """
    return _litestar_normalize()(f"{litestar_mount(app, handler)}{remaining}")


def litestar_mount(app: Any, handler: Any) -> str:  # noqa: ANN401
    """Return the path a Litestar app mounts `handler` at, router prefixes included.

    Raises:
        RuntimeError: If the router lists no mount for the handler.
    """
    mounts = app.asgi_router._mount_routes  # noqa: SLF001
    for mount, node in mounts.items():
        if any(entry[1] is handler for entry in node.asgi_handlers.values()):
            return mount
    msg = (
        "Litestar routed the request to a mount its router does not list, "
        "so the path it serves is unknown."
    )
    raise RuntimeError(msg)


def starlette_route_path(
    path: Annotated[str, Doc("The request path, prefix included.")],
    root_path: Annotated[str, Doc("The prefix a mount or a proxy added.")],
) -> str:
    """Return the path Starlette's router matches, as it reads it.

    The root path is taken off whole and as given, only where the path
    starts with it at a segment boundary, and a path equal to it reads as
    empty. A path that does not start with it is read whole.
    """
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


@functools.cache
def _litestar_normalize() -> Callable[[str], str]:
    """Return how Litestar normalizes a path, looked up once."""
    from litestar.utils import normalize_path  # noqa: PLC0415

    return normalize_path


def _scope_text(value: str | bytes) -> str:
    """Return an ASGI text or byte value without losing byte identity."""
    return value.decode("latin-1") if isinstance(value, bytes) else value


def _request_scheme(scope: MutableMapping[str, Any]) -> str:
    """Return the lower-case scheme of an ASGI request."""
    return _scope_text(scope.get("scheme", "http")).lower()


def _request_root_path(scope: MutableMapping[str, Any]) -> str:
    """Return the mount or proxy prefix in canonical ASGI form."""
    return _scope_text(scope.get("root_path", "")).rstrip("/")


def _canonical_host(host: str) -> tuple[str, bool]:
    """Return a normalized host and whether it is an IPv6 literal."""
    try:
        address = ip_address(host)
    except ValueError:
        return host.lower(), False
    return str(address), isinstance(address, IPv6Address)


def _canonical_port(port: Any, scheme: str) -> int | object | None:  # noqa: ANN401
    """Return a normalized non-default port, or mark a malformed one."""
    if port is None:
        return None
    if isinstance(port, int):
        number = port
    else:
        value = _scope_text(port)
        if not value.isascii() or not value.isdecimal():
            return _INVALID_PORT
        number = int(value)
    if not 0 <= number <= _MAX_PORT:
        return _INVALID_PORT
    return None if number == _DEFAULT_PORTS.get(scheme) else number


def _canonical_authority(  # noqa: PLR0911
    authority: str, scheme: str
) -> str:
    """Return one authority with canonical host casing, IP, and port."""
    raw = authority.strip()
    if not raw:
        return ""
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return raw.lower()
        host = raw[1:closing]
        suffix = raw[closing + 1 :]
        if suffix and not suffix.startswith(":"):
            return raw.lower()
        port = _canonical_port(suffix[1:] if suffix else None, scheme)
        if port is _INVALID_PORT:
            return raw.lower()
        bracketed = True
    elif raw.count(":") == 1:
        host, raw_port = raw.rsplit(":", 1)
        port = _canonical_port(raw_port, scheme)
        if port is _INVALID_PORT:
            return raw.lower()
        bracketed = False
    else:
        host = raw
        port = None
        bracketed = False
    if not host:
        return raw.lower()
    normalized, ipv6 = _canonical_host(host)
    rendered = f"[{normalized}]" if bracketed or ipv6 else normalized
    return rendered if port is None else f"{rendered}:{port}"


def _request_authority(scope: MutableMapping[str, Any]) -> str:
    """Return the canonical authority used to route an ASGI request.

    The Host header is what HTTP host routing reads, so it takes precedence.
    A server tuple is the ASGI fallback when that header is absent. Forwarded
    headers are deliberately ignored: only trusted proxy middleware may turn
    those into the scope values used here.
    """
    scheme = _request_scheme(scope)
    for raw_name, raw_value in scope.get("headers", ()):
        if _scope_text(raw_name).lower() == "host":
            return _canonical_authority(_scope_text(raw_value), scheme)
    server = scope.get("server")
    if not server:
        return ""
    host = _scope_text(server[0])
    port = _canonical_port(server[1] if len(server) > 1 else None, scheme)
    normalized, ipv6 = _canonical_host(host)
    rendered = f"[{normalized}]" if ipv6 else normalized
    if port is _INVALID_PORT:
        raw_port = server[1]
        if isinstance(raw_port, bytes):
            raw_port = raw_port.decode("latin-1")
        return f"{rendered}:{raw_port}"
    return rendered if port is None else f"{rendered}:{port}"


def _routing_app(app: Any) -> Any:  # noqa: ANN401
    """Unwrap ASGI middleware until an application exposing routes is reached."""
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        bound = _bound_router(app)
        if bound is not None:
            app = bound
            continue
        if _is_mount(app) or _is_route(app):
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


def _is_mount(app: Any) -> bool:  # noqa: ANN401
    """Return whether `app` is a Starlette `Mount` routing node."""
    return any(
        klass.__module__ == "starlette.routing" and klass.__name__ == "Mount"
        for klass in type(app).__mro__
    )


def _is_route(app: Any) -> bool:  # noqa: ANN401
    """Return whether `app` is a Starlette leaf routing node."""
    return any(
        klass.__module__ == "starlette.routing"
        and klass.__name__ in {"Route", "WebSocketRoute"}
        for klass in type(app).__mro__
    )


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


def read_route(scope: MutableMapping[str, Any]) -> str | None:
    """Return the route the route reader in the scope names, `None` without one."""
    reading = scope.get(ROUTE_READER_KEY)
    if reading is None:
        return None
    reader, root_path, path = reading
    return reader(scope, root_path, path, None)


def route_template(
    scope: MutableMapping[str, Any], status: int | None = None
) -> str | None:
    """Return the route template of the request, when there is one.

    A refused request reads the route its refusal named. A request to an
    installed app reads the route its route reader names, through several
    installed apps the outermost one's. A request a router reached names
    the route its request span names, and `status` names the route a slash
    redirect sends it to. One answered before routing, such as a refusal,
    a cached response or a CORS preflight, names the route its path would
    reach. A request to an app nothing installed names no route.
    """
    if ROUTE_KEY in scope:
        return scope[ROUTE_KEY]
    reading = scope.get(ROUTE_READER_KEY)
    if reading is None:
        return None
    reader, root_path, path = reading
    return reader(scope, root_path, path, status, reach=True)


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

    from grelmicro.integrations import (  # noqa: PLC0415
        _fastapi_internals as fastapi,
    )

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
        inner = getattr(candidate, "routes", None) or getattr(
            _routing_app(getattr(candidate, "app", None)), "routes", None
        )
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
    route = matched is not None and not isinstance(matched, Mount)
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


def as_patterns(
    value: Annotated[
        tuple[str, ...] | list[str],
        Doc("What the caller passed as a set of path patterns."),
    ],
    *,
    name: Annotated[str, Doc("The parameter's name, for the message.")],
) -> tuple[str, ...]:
    """Return the patterns as a tuple, refusing a bare string.

    A string is a sequence of characters, so one passed here would be
    walked one character at a time: `exclude="/internal/*"` ends in `*`,
    which matches every path as a prefix, and the middleware would then
    act on nothing at all. It is a missing comma, and it fails silently,
    so it is refused where it is written instead.

    Raises:
        TypeError: If `value` is a string.
    """
    if isinstance(value, str):
        msg = (
            f"{name}={value!r} is a string, and a set of path patterns is "
            f"expected. Write it as a tuple: {name}=({value!r},)."
        )
        raise TypeError(msg)
    return tuple(value)


def matches(
    path: Annotated[
        str, Doc("The request path, as the ASGI scope carries it.")
    ],
    patterns: Annotated[
        tuple[str, ...],
        Doc("Patterns to match it against."),
    ],
) -> bool:
    """Return whether one pattern matches this path.

    Exact, unless the pattern ends with `*`, which matches as a prefix. A
    router mounted under `/payments` is therefore selected by
    `"/payments/*"`, which is how FastAPI, Starlette and Litestar apps
    group endpoints in the first place.
    """
    return any(_matches_one(path, pattern) for pattern in patterns)


def _matches_one(path: str, pattern: str) -> bool:
    """Return whether one pattern matches this path.

    A prefix pattern matches the prefix itself as well as what sits under
    it, so `"/payments/*"` covers `POST /payments`, the create route of
    the very router it names.
    """
    if not pattern.endswith(_PREFIX):
        return path == pattern
    prefix = pattern[: -len(_PREFIX)]
    return path.startswith(prefix) or path == prefix.rstrip("/")


def names_route(
    pattern: Annotated[str, Doc("A pattern a component was given.")],
    template: Annotated[str, Doc("The path a route is declared under.")],
    regex: Annotated[
        Pattern[str],
        Doc("The regex a URL is matched against for that path."),
    ],
) -> bool:
    """Return whether this pattern can select a request this route answers.

    A pattern is matched against the URL a request asks for, and a route
    is declared as a template that stands for many. So `"/users/me"`
    names `GET /users/{uid}`, and asking the template alone answers no.

    Every place that reasons about a pattern and a route has to agree on
    this. The response cache refuses a pattern naming a read behind a
    security scheme, and a refusal that asked the template alone would
    let the URL through and answer over the gate.
    """
    if pattern.endswith(_PREFIX):
        return _prefix_names(pattern[: -len(_PREFIX)], template)
    if template == pattern:
        return True
    return bool(regex.fullmatch(pattern))


def _prefix_names(under: str, template: str) -> bool:
    """Return whether any URL under `under` is one this template answers.

    Compared segment by segment rather than by asking the compiled regex
    about a made-up URL. A prefix names a set of URLs, and no single
    string stands for that set: one built by appending a character
    answers only for a remainder one segment long, and one built from
    the prefix alone answers only for the shortest member.

    A parameter stands for whatever the prefix put in its place, so
    `/users/me/*` names `/users/{uid}/settings`. The last segment of the
    prefix may cut into a segment of the template, because a prefix is
    matched against the URL rather than against a boundary, so
    `/products/co*` names `/products/cold`. A literal glued to a `path`
    parameter, such as `/raw{path:path}`, absorbs every segment of a
    prefix starting with that literal.
    """
    boundary = under.endswith("/")
    parts = under.rstrip("/").split("/")
    declared = template.split("/")
    for index, part in enumerate(parts):
        if index >= len(declared):
            return False
        against = declared[index]
        if against.startswith("{"):
            if ":path}" in against:
                # A path converter absorbs every remaining segment, but only
                # after the literal and typed segments before it agreed.
                return True
            continue
        head, brace, rest = against.partition("{")
        if brace and ":path}" in rest and part.startswith(head):
            return True
        if index == len(parts) - 1 and not boundary:
            # The prefix may stop inside this one.
            if not against.startswith(part):
                return False
        elif against != part:
            return False
    return True


def selects(
    path: Annotated[
        str, Doc("The request path, as the ASGI scope carries it.")
    ],
    *,
    include: Annotated[
        tuple[str, ...],
        Doc("Paths to act on. Empty means every path."),
    ],
    exclude: Annotated[
        tuple[str, ...],
        Doc("Paths to leave alone, whatever `include` says."),
    ],
) -> bool:
    """Return whether a middleware acts on this path.

    `exclude` wins, so a service can name a router and carve one route out
    of it without the two rules fighting.
    """
    if matches(path, exclude):
        return False
    return not include or matches(path, include)
