"""Which paths a middleware acts on.

One matching rule for every grelmicro HTTP middleware, so a reader learns it
once: `include` narrows, `exclude` carves out, and `exclude` wins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BeforeValidator
from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from re import Pattern

__all__ = [
    "BARE_METHOD_MESSAGE",
    "BARE_NAME_MESSAGE",
    "BARE_STRING_MESSAGE",
    "MALFORMED_JSON_MESSAGE",
    "FieldNames",
    "MethodNames",
    "PathPatterns",
    "as_patterns",
    "matches",
    "names_route",
    "refuse_bare_method",
    "refuse_bare_name",
    "refuse_bare_string",
    "route_path",
    "selects",
    "walk_routes",
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


def route_path(
    scope: Annotated[
        MutableMapping[str, Any], Doc("The ASGI scope of the request.")
    ],
) -> str:
    """Return the path the app declares its routes under.

    `scope["path"]` carries the prefix a mount or a proxy adds, and
    `root_path` carries that prefix, so what is left is the path the route
    is written with. A pattern is therefore the same in an app served at
    the root, mounted under another app, or behind a proxy, and the same
    as the path the OpenAPI schema publishes.

    Only a whole segment is a prefix, so a `root_path` of `/api` leaves
    `/apikeys` alone and shortens `/api/keys`. An app answering at its
    prefix reads as `/`, which is the route it declares.
    """
    path = scope["path"]
    root = scope.get("root_path", "").rstrip("/")
    if not root or not path.startswith(root):
        return path
    if path == root:
        return "/"
    if path[len(root)] == "/":
        return path[len(root) :]
    return path


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
        router = getattr(app, "router", None)
        if hasattr(app, "routes") or hasattr(router, "routes"):
            return app
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


def _is_exception_middleware(app: Any) -> bool:  # noqa: ANN401
    """Return whether `app` is Starlette's built-in exception router."""
    klass = type(app)
    return (
        klass.__module__ == "starlette.middleware.exceptions"
        and klass.__name__ == "ExceptionMiddleware"
    )


def _is_starlette_routing_app(app: Any) -> bool:  # noqa: ANN401
    """Return whether `app` resolves to a Starlette-compatible router."""
    routed = _routing_app(app)
    if routed is None:
        return False
    if _is_mount(routed) or _is_route(routed):
        return True
    return any(
        (
            klass.__module__ == "starlette.applications"
            and klass.__name__ == "Starlette"
        )
        or (
            klass.__module__ == "starlette.routing"
            and klass.__name__ == "Router"
        )
        for klass in type(routed).__mro__
    )


def _wrapped_app(app: Any) -> Any | None:  # noqa: ANN401
    """Return the application an explicit ASGI wrapper delegates to."""
    if _is_mount(app) or _is_route(app):
        return None
    nested = getattr(app, "app", None)
    if (
        nested is None
        or nested is app
        or getattr(nested, "__self__", None) is app
    ):
        return None
    return nested


def _transparent_routing_source(app: Any) -> Any:  # noqa: ANN401
    """Unwrap bound routers and Starlette's built-in exception router."""
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        bound = _bound_router(app)
        if bound is not None:
            app = bound
            continue
        if not _is_exception_middleware(app):
            break
        nested = _wrapped_app(app)
        if nested is None:
            break
        app = nested
    return app


def _has_configured_middleware(app: Any) -> bool:  # noqa: ANN401
    """Return whether a routing application declares middleware of its own."""
    app = _transparent_routing_source(app)
    if _wrapped_app(app) is not None:
        return True
    if getattr(app, "user_middleware", ()):
        return True
    routed = getattr(app, "router", None) or app
    stack = getattr(routed, "middleware_stack", None)
    endpoint = getattr(routed, "app", None)
    return stack is not None and endpoint is not None and stack != endpoint


def _middleware_boundaries(
    app: Any,  # noqa: ANN401
    *,
    include_root: bool = False,
) -> set[tuple[str, bool]]:
    """Return exact or nested paths a parent response cache must not cross."""
    app = _transparent_routing_source(app)
    if _wrapped_app(app) is not None:
        return {("", True)}
    if include_root and _has_configured_middleware(app):
        return {("", True)}
    found: set[tuple[str, bool]] = set()
    _visit_middleware_boundaries(app, "", frozenset(), found)
    return found


def _visit_middleware_boundaries(
    current: Any,  # noqa: ANN401
    prefix: str,
    ancestors: frozenset[int],
    found: set[tuple[str, bool]],
) -> None:
    """Add middleware boundaries below one routing application."""
    current = _transparent_routing_source(current)
    if current is None or id(current) in ancestors:
        return
    nested_ancestors = ancestors | {id(current)}
    if _is_mount(current):
        _visit_boundary_app(
            getattr(current, "app", current),
            f"{prefix}{getattr(current, 'path', '')}",
            nested_ancestors,
            found,
        )
        return
    if _is_route(current):
        _visit_boundary_route(current, prefix, nested_ancestors, found)
        return
    router = getattr(current, "router", None)
    for route in getattr(router or current, "routes", ()) or ():
        _visit_boundary_route(route, prefix, nested_ancestors, found)


def _visit_boundary_app(
    app: Any,  # noqa: ANN401
    path: str,
    ancestors: frozenset[int],
    found: set[tuple[str, bool]],
) -> None:
    """Add or descend through an application-wide middleware boundary."""
    if _has_configured_middleware(app):
        found.add((path, True))
    else:
        _visit_middleware_boundaries(app, path, ancestors, found)


def _visit_boundary_route(
    route: Any,  # noqa: ANN401
    prefix: str,
    ancestors: frozenset[int],
    found: set[tuple[str, bool]],
) -> None:
    """Inspect one included router, mount, or leaf route for middleware."""
    included = getattr(route, "original_router", None)
    if included is not None:
        context = getattr(route, "include_context", None)
        _visit_boundary_app(
            included,
            f"{prefix}{getattr(context, 'prefix', '')}",
            ancestors,
            found,
        )
        return
    path = f"{prefix}{getattr(route, 'path', '')}"
    nested = getattr(route, "app", route)
    if getattr(route, "routes", None) is not None:
        _visit_boundary_app(nested, path, ancestors, found)
    elif _has_configured_middleware(nested):
        found.add((path, False))
    else:
        routed = _nested_routing_app(route)
        if routed is None:
            return
        nested_boundaries: set[tuple[str, bool]] = set()
        _visit_boundary_app(routed, "", ancestors, nested_boundaries)
        if nested_boundaries:
            # A Router used as a Route endpoint receives the outer route's
            # unchanged scope. Its own path cannot be composed with the
            # outer one the way a Mount's can, so refuse the exact path.
            found.add((path, False))


def _nested_routing_app(route: Any) -> Any | None:  # noqa: ANN401
    """Return a routing application used as a leaf route endpoint."""
    if getattr(route, "routes", None) is not None:
        return None
    nested = getattr(route, "app", None)
    if nested is None:
        return None
    routed = _routing_app(nested)
    if routed is None or routed is route:
        return None
    router = getattr(routed, "router", None)
    return (
        routed
        if hasattr(routed, "routes") or hasattr(router, "routes")
        else None
    )


def _route_source(app: Any, *, unwrap_middleware: bool) -> Any | None:  # noqa: ANN401
    """Resolve the routing object visible through the requested boundary."""
    app = _transparent_routing_source(app)
    if unwrap_middleware:
        return _routing_app(app)
    if _wrapped_app(app) is not None:
        return None
    return app


def _walk_mounted_routes(
    app: Any,  # noqa: ANN401
    prefix: str,
    ancestors: frozenset[int],
    *,
    unwrap_middleware: bool,
) -> list[tuple[str, Any, tuple[Any, ...]]] | None:
    """Walk a direct `Mount`, or return `None` for another routing object."""
    if not _is_mount(app):
        return None
    nested = getattr(app, "app", app)
    if not unwrap_middleware and _has_configured_middleware(nested):
        return []
    return _walk_routes(
        nested,
        f"{prefix}{getattr(app, 'path', '')}",
        (),
        ancestors,
        unwrap_middleware=unwrap_middleware,
    )


def _walk_routes(
    app: Any,  # noqa: ANN401
    prefix: str,
    contexts: tuple[Any, ...],
    ancestors: frozenset[int],
    *,
    unwrap_middleware: bool,
) -> list[tuple[str, Any, tuple[Any, ...]]]:
    """Walk one routing branch while stopping only its own ancestry."""
    app = _route_source(app, unwrap_middleware=unwrap_middleware)
    if app is None or id(app) in ancestors:
        return []
    nested_ancestors = ancestors | {id(app)}
    mounted = _walk_mounted_routes(
        app,
        prefix,
        nested_ancestors,
        unwrap_middleware=unwrap_middleware,
    )
    if mounted is not None:
        return mounted
    if _is_route(app):
        return (
            [(prefix, app, contexts)]
            if getattr(app, "path", None) is not None
            else []
        )
    own = getattr(app, "router", None)
    if own is not None:
        contexts = (*contexts, own)
    found: list[tuple[str, Any, tuple[Any, ...]]] = []
    for route in getattr(app, "routes", ()):
        context = getattr(route, "include_context", None)
        included = getattr(route, "original_router", None)
        if included is not None:
            found.extend(
                _walk_routes(
                    included,
                    f"{prefix}{getattr(context, 'prefix', '')}",
                    (*contexts, context, included),
                    nested_ancestors,
                    unwrap_middleware=unwrap_middleware,
                )
            )
            continue
        inner = getattr(route, "routes", None)
        if inner or (unwrap_middleware and inner is not None):
            nested = getattr(route, "app", route)
            if not unwrap_middleware and _has_configured_middleware(nested):
                continue
            found.extend(
                _walk_routes(
                    nested,
                    f"{prefix}{getattr(route, 'path', '')}",
                    (),
                    nested_ancestors,
                    unwrap_middleware=unwrap_middleware,
                )
            )
            continue
        if getattr(route, "path", None) is not None:
            found.append((prefix, route, contexts))
    return found


def _dependency_topology(
    dependency: Any,  # noqa: ANN401
    provider: Any = None,  # noqa: ANN401
    ancestors: frozenset[int] = frozenset(),
) -> tuple[Any, ...]:
    """Return the identity and descendants of one resolved dependency."""
    if dependency is None:
        return ()
    if id(dependency) in ancestors:
        return ("cycle", id(dependency))
    nested_ancestors = ancestors | {id(dependency)}
    call, effective, provider = _effective_dependency_call(dependency, provider)
    return (
        id(dependency),
        id(call),
        tuple(
            _dependency_topology(child, provider, nested_ancestors)
            for child in getattr(dependency, "dependencies", ()) or ()
        ),
        id(effective),
        _dependency_overrides_topology(provider),
    )


def _dependency_callable(dependency: Any) -> Any:  # noqa: ANN401
    """Return the callable stored on a resolved or declared dependency."""
    if hasattr(dependency, "call"):
        return getattr(dependency, "call", None)
    return getattr(dependency, "dependency", None)


def _dependency_overrides_provider(
    holder: Any,  # noqa: ANN401
    inherited: Any = None,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Return the closest FastAPI dependency override provider."""
    provider = getattr(holder, "dependency_overrides_provider", None)
    return inherited if provider is None else provider


def _dependency_overrides_topology(
    provider: Any,  # noqa: ANN401
) -> tuple[Any, ...]:
    """Return identities that change when FastAPI overrides change."""
    if provider is None:
        return ()
    overrides = getattr(provider, "dependency_overrides", None)
    if overrides is None:
        return (id(provider),)
    entries = tuple(
        sorted((id(key), id(value)) for key, value in overrides.items())
    )
    return id(provider), id(overrides), entries


def _effective_dependency_call(
    dependency: Any,  # noqa: ANN401
    provider: Any = None,  # noqa: ANN401
) -> tuple[Any, Any, Any]:
    """Return a dependency's declared call, effective call, and provider."""
    provider = _dependency_overrides_provider(dependency, provider)
    call = _dependency_callable(dependency)
    overrides = getattr(provider, "dependency_overrides", None)
    if overrides is None:
        return call, call, provider
    effective = overrides.get(call, call)
    return call, effective, provider


def _declared_dependency_topology(holder: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """Return dependencies declared directly on a router or include."""
    provider = _dependency_overrides_provider(holder)
    found: list[tuple[Any, ...]] = []
    for dependency in getattr(holder, "dependencies", ()) or ():
        call, effective, dependency_provider = _effective_dependency_call(
            dependency, provider
        )
        found.append(
            (
                id(dependency),
                id(call),
                id(effective),
                _dependency_overrides_topology(dependency_provider),
            )
        )
    return tuple(found)


def _middleware_topology(app: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """Return middleware declarations and the currently built stack."""
    declared = tuple(
        (
            id(middleware),
            id(getattr(middleware, "cls", middleware)),
        )
        for middleware in getattr(app, "user_middleware", ()) or ()
    )
    stack = getattr(app, "middleware_stack", None)
    chain: list[tuple[int, type[Any]]] = []
    seen: set[int] = set()
    while stack is not None and id(stack) not in seen:
        seen.add(id(stack))
        chain.append((id(stack), type(stack)))
        stack = _wrapped_app(stack)
    return declared, tuple(chain)


def _route_topology_node(  # noqa: PLR0911
    current: Any,  # noqa: ANN401
    ancestors: frozenset[int],
) -> tuple[Any, ...]:
    """Return a cycle-safe snapshot of one routing branch."""
    if current is None:
        return ("none",)
    bound = _bound_router(current)
    if bound is not None:
        current = bound
    if id(current) in ancestors:
        return ("cycle", id(current))
    nested_ancestors = ancestors | {id(current)}
    included = getattr(current, "original_router", None)
    if included is not None:
        context = getattr(current, "include_context", None)
        return (
            "include",
            id(current),
            getattr(context, "prefix", ""),
            _declared_dependency_topology(context),
            _route_topology_node(included, nested_ancestors),
        )
    if _is_mount(current):
        return (
            "mount",
            id(current),
            getattr(current, "path", ""),
            _route_topology_node(
                getattr(current, "app", None), nested_ancestors
            ),
        )
    if _is_route(current):
        dependency = getattr(current, "dependant", None)  # codespell:ignore
        provider = _dependency_overrides_provider(current)
        return (
            "route",
            id(current),
            getattr(current, "path", ""),
            tuple(sorted(getattr(current, "methods", None) or ())),
            _dependency_overrides_topology(provider),
            _dependency_topology(dependency, provider),
            _route_topology_node(
                getattr(current, "app", None), nested_ancestors
            ),
        )
    nested = _wrapped_app(current)
    if nested is not None:
        return (
            "wrapper",
            id(current),
            type(current),
            _route_topology_node(nested, nested_ancestors),
        )
    router = getattr(current, "router", None)
    routed = router or current
    routes = getattr(routed, "routes", None)
    if routes is None:
        return ("opaque", id(current), type(current))
    return (
        "router",
        id(current),
        id(routed),
        _middleware_topology(current),
        _middleware_topology(routed),
        _declared_dependency_topology(current),
        _declared_dependency_topology(routed),
        tuple(
            _route_topology_node(route, nested_ancestors)
            for route in routes or ()
        ),
    )


def _route_topology(app: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """Return a cycle-safe snapshot that changes with routing policy."""
    return _route_topology_node(app, frozenset())


def walk_routes(
    app: Annotated[  # noqa: ANN401
        Any, Doc("The application, or the router, to read the routes off.")
    ],
    prefix: Annotated[str, Doc("What the routes below sit under.")] = "",
    contexts: Annotated[
        tuple[Any, ...],
        Doc("The include contexts above them, outermost first."),
    ] = (),
    *,
    unwrap_middleware: Annotated[
        bool,
        Doc("Whether to inspect routes behind mounted ASGI middleware."),
    ] = False,
) -> list[tuple[str, Any, tuple[Any, ...]]]:
    """Return every route the app declares, with the path it sits under.

    A route is `(prefix, route, contexts)`, where the prefix is what the
    mounts and the routers above it add to the path it was written with,
    and the contexts are what was declared above it, outermost first:
    the app's own router, each inclusion, and the router it included.

    An included router is a node of its own rather than the routes it
    holds, so what it was included under has to be carried down to them
    from here. A mount starts a new application boundary: the parent
    router's dependencies do not apply inside it. Middleware around a
    mounted app is a boundary too unless the caller explicitly asks to
    inspect through it.
    """
    return _walk_routes(
        app,
        prefix,
        contexts,
        frozenset(),
        unwrap_middleware=unwrap_middleware,
    )


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
        Pattern[str] | None,
        Doc("What the router compiled that path into, if anything."),
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

    A route the router could not compile carries no regex, and is
    answered by its template alone, which is all there is to compare.
    """
    if pattern.endswith(_PREFIX):
        return _prefix_names(pattern[: -len(_PREFIX)], template)
    if template == pattern:
        return True
    return regex is not None and bool(regex.fullmatch(pattern))


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
    `/products/co*` names `/products/cold`.
    """
    parts = under.rstrip("/").split("/")
    declared = template.split("/")
    if len(parts) > len(declared):
        # Only a converter that spans separators reaches past the
        # segments the template declares, `{rest:path}` and nothing else.
        return declared[-1].startswith("{") and ":path}" in declared[-1]
    for index, part in enumerate(parts):
        against = declared[index]
        if against.startswith("{"):
            continue
        if index == len(parts) - 1:
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
