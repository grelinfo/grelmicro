"""Which paths a middleware acts on.

One matching rule for every grelmicro HTTP middleware, so a reader learns it
once: `include` narrows, `exclude` carves out, and `exclude` wins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import MutableMapping

__all__ = ["as_patterns", "matches", "route_path", "selects", "walk_routes"]

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


def walk_routes(
    app: Annotated[  # noqa: ANN401
        Any, Doc("The application, or the router, to read the routes off.")
    ],
    prefix: Annotated[str, Doc("What the routes below sit under.")] = "",
    contexts: Annotated[
        tuple[Any, ...],
        Doc("The include contexts above them, outermost first."),
    ] = (),
) -> list[tuple[str, Any, tuple[Any, ...]]]:
    """Return every route the app declares, with the path it sits under.

    A route is `(prefix, route, contexts)`, where the prefix is what the
    mounts and the routers above it add to the path it was written with,
    and the contexts are those routers' inclusions, outermost first.

    An included router is a node of its own rather than the routes it
    holds, so what it was included under has to be carried down to them
    from here. A mount is walked the same way.
    """
    found: list[tuple[str, Any, tuple[Any, ...]]] = []
    for route in getattr(app, "routes", ()):
        context = getattr(route, "include_context", None)
        included = getattr(route, "original_router", None)
        if included is not None:
            found.extend(
                walk_routes(
                    included,
                    f"{prefix}{getattr(context, 'prefix', '')}",
                    (*contexts, context),
                )
            )
            continue
        inner = getattr(route, "routes", None)
        if inner:
            found.extend(
                walk_routes(
                    route,
                    f"{prefix}{getattr(route, 'path', '')}",
                    contexts,
                )
            )
            continue
        if getattr(route, "path", None) is not None:
            found.append((prefix, route, contexts))
    return found


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
