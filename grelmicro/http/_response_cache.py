"""Response cache for HTTP reads."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections import OrderedDict, abc

# Imported at runtime, not under `TYPE_CHECKING`: it appears in a config
# field's annotation, which pydantic resolves from module globals.
from collections.abc import Mapping
from dataclasses import dataclass
from logging import getLogger
from time import time as clock_time
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Self,
    TypedDict,
    cast,
)

from pydantic import AfterValidator, BaseModel, PositiveInt
from typing_extensions import Doc

from grelmicro._config import (
    Live,
    Reconfigurable,
    build_config,
    env_prefixes,
    resolve_config,
)
from grelmicro._paths import (
    _PREFIX,
    BARE_STRING_MESSAGE,
    FieldNames,
    PathPatterns,
    _bound_router,
    _effective_dependency_call,
    _is_starlette_routing_app,
    _middleware_boundaries,
    _nested_routing_app,
    _route_topology,
    _routing_app,
    as_patterns,
    matches,
    names_route,
    route_path,
    walk_routes,
)
from grelmicro.cache._stampede import compute_with_stampede
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.http._conditional import (
    _KEPT_ON_304,
    _matches_weak,
    _tags,
    etag_of,
)
from grelmicro.http._idempotency import (
    StoredResponse,
    _authenticated_scope,
    _authentication_paths,
)

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        MutableMapping,
        Sequence,
    )
    from re import Pattern
    from types import TracebackType

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "CachedResponses",
    "CachedResponsesMiddleware",
]

logger = getLogger("grelmicro.http.cache")

_MARKER = "__grelmicro_response_cache__"
"""Attribute the declared dependency carries, holding the route's TTL."""

_UNMARKED: Any = object()
"""Marks a route that declared nothing, which a `None` TTL cannot."""

_SAFE_METHODS = frozenset({"GET", "HEAD"})
"""Methods a response cache answers. Everything else passes through."""

_HTTP_200_OK = 200
"""The one status a response cache stores."""

_HTTP_304_NOT_MODIFIED = 304
"""Status answering a read whose entity tag the client already holds."""

_DEFAULT_TTL = 60.0
"""Seconds a response is kept when neither the route nor the component says."""

_DEFAULT_MAX_BODY_SIZE = 1024 * 1024
"""Largest response body held in memory to store, in bytes."""

_PRIVATE_REQUEST_HEADERS = (b"authorization", b"cookie")
"""Request headers that make a response one caller's, never a cache's."""

_PARTIAL_REQUEST_HEADER = b"range"
"""Header asking for part of a resource, which a stored whole is not."""

_UNCACHEABLE_DIRECTIVES = frozenset({"no-store", "no-cache", "private"})
"""`Cache-Control` directives that refuse the store outright."""

_UNCACHEABLE_RESPONSE_HEADERS = frozenset({b"set-cookie", b"content-encoding"})
"""Response headers that make it one caller's, or not the body it says."""

_UNSTORABLE_LIMIT = 512
"""How many keys are remembered as ones nothing is ever stored under."""

_REPORT_INTERVAL = 60.0
"""Seconds between two reports that the store could not be reached."""

_WARNED_LIMIT = 128
"""How many distinct `Vary` refusals are remembered before warning again."""


class _Entry(TypedDict):
    """A stored response as it rides the cache.

    Header values and the body are `latin-1` strings, which round-trip any
    byte sequence through the cache serializers without loss. `stored_at`
    is what `Age` counts from.
    """

    status: int
    headers: Sequence[Sequence[str]]
    body: str
    stored_at: float
    kept: float


NOT_SECONDS = "is not a number of seconds a response is kept for."
"""Why a lifetime was refused, without repeating what it was.

`SettingsValidationError` takes the rejected value back out of the
message, so a validator that named it would be quoting a blank.
"""

_LEAVE_THE_PATH_OUT = (
    " Leave the path out, or name it in exclude=, to cache it not at all."
)
"""How to say `never` about one path, which is not what a zero says.

Only for a pattern. A component-wide `ttl` has no path to leave out, and
telling its operator to find one sends them looking for something that
is not there.
"""


def _seconds(value: float) -> float:
    """Return `value`, refusing a lifetime a response cannot be kept for.

    Written as `not value > 0` rather than `value <= 0`, so a NaN is
    refused too. Every comparison with a NaN is false, so the negated
    test is the one that turns it away.

    Raises:
        ValueError: If it is not a positive number of seconds.
    """
    if not value > 0:
        msg = f"ttl {NOT_SECONDS}"
        raise ValueError(msg)
    return value


def _seconds_per_path(
    value: tuple[str, ...] | Mapping[str, float],
) -> tuple[str, ...] | Mapping[str, float]:
    """Return `value`, refusing a pattern that names an impossible lifetime.

    Raises:
        ValueError: If a pattern names a lifetime that is not a positive
            number of seconds. Located by its position, never by the
            pattern: this mapping can arrive from a mounted source, and
            a key there is as much operator input as a value, which is
            why the reload path keeps key names out of its logs too.
    """
    if isinstance(value, abc.Mapping):
        for position, ttl in enumerate(value.values(), start=1):
            if not ttl > 0:
                msg = (
                    f"pattern {position} in include "
                    f"{NOT_SECONDS}{_LEAVE_THE_PATH_OUT}"
                )
                raise ValueError(msg)
    return value


def check_ttl(
    ttl: Annotated[float | None, Doc("What was given as a lifetime.")],
    where: Annotated[str, Doc("The argument's name, for the message.")],
) -> None:
    """Refuse a lifetime a response cannot be kept for.

    Raises:
        ValueError: If `ttl` is not a positive number of seconds.
    """
    if ttl is None or ttl > 0:
        return
    msg = (
        f"{where}={ttl!r} is not a number of seconds a response is kept "
        "for. Leave the path out, or name it in exclude=, to cache it "
        "not at all."
    )
    raise ValueError(msg)


def declare_cached(
    ttl: Annotated[
        float | None, Doc("Seconds the route's response is served from.")
    ],
) -> Callable[[], Awaitable[None]]:
    """Return the callable a route declares to have its response cached.

    `grelmicro.integrations.fastapi.CachedResponse` wraps it in a
    `Depends`, which is how a route says so, and `micro.install(app)`
    reads it back off the dependency tree. It computes nothing: what it
    carries is the TTL, and where it is declared.

    Raises:
        ValueError: If `ttl` is not a positive number of seconds.
    """
    check_ttl(ttl, "ttl")

    async def cached_response() -> None:
        """Declare that this route's response is cached.

        Async so the framework resolves it on the event loop. A sync
        dependency goes through a worker thread, and this one is a
        declaration with nothing in it to run there.
        """

    setattr(cached_response, _MARKER, ttl)
    return cached_response


class _Unset:
    """Stands for an argument the caller did not pass.

    `None` cannot: it is what `vary_by_query` means by "key on the whole
    query string", so a caller writing it has said something, and
    `resolve_config` reads a `None` keyword as one nobody passed. Without
    a sentinel the environment would answer over an argument the code
    wrote, which is the one thing the resolution order never allows.
    """

    def __repr__(self) -> str:
        """Return the name it is published under.

        The default appears in the signature the API reference renders
        and an editor completes from, and an object's address there says
        nothing and changes every run.
        """
        return "UNSET"


UNSET = _Unset()
"""The one instance of `_Unset`, so a caller can be told apart from a default."""


def _unique_policy_sources(
    sources: tuple[tuple[Any, bool], ...],
) -> tuple[tuple[Any, bool], ...]:
    """Return applications once each, retaining the strictest root boundary."""
    found: list[tuple[Any, bool]] = []
    for app, include_root in sources:
        for index, (seen, strict) in enumerate(found):
            if app is seen:
                found[index] = (seen, strict or include_root)
                break
        else:
            found.append((app, include_root))
    return tuple(found)


class _Policies:
    """The paths a middleware caches, and for how long.

    Built from two sources that answer the same question. `include` names
    URLs and is written where the component is registered. The routes come
    from `@cache_response`, and are read off the app at install and again
    when the app starts, so a route added after `install` counts too.
    """

    __slots__ = (
        "_app",
        "_base_sources",
        "_exclude",
        "_include",
        "_refused",
        "_refused_paths",
        "_routes",
        "_topology",
    )

    def __init__(
        self,
        include: Annotated[
            Mapping[str, float] | Sequence[str],
            Doc(
                "The paths cached. A sequence keeps each for the "
                "component's own TTL, and a mapping gives each its own."
            ),
        ],
        exclude: Annotated[
            tuple[str, ...],
            Doc("The paths carved out again, whatever `include` says."),
        ] = (),
    ) -> None:
        """Hold the path rules, with no routes read yet.

        A bare string is refused before this: by the config on the
        component's door, and by the middleware on the hand-wired one.

        Raises:
            ValueError: If a pattern names a lifetime a response cannot
                be kept for.
        """
        # A sequence names the paths and leaves the seconds to the
        # component, which is how every other middleware reads. `None`
        # stands for "whatever the component says", the same as a route
        # that declared no seconds of its own.
        items: list[tuple[str, float | None]] = (
            [(pattern, None) for pattern in include]
            if not isinstance(include, abc.Mapping)
            else list(include.items())
        )
        for pattern, ttl in items:
            if ttl is not None:
                check_ttl(ttl, f"include[{pattern!r}]")
        # Most specific first: an exact path beats a prefix, and a longer
        # prefix beats the shorter one it sits under, so a rule written
        # for one route is not answered by the one written for its router.
        self._include: tuple[tuple[str, float | None], ...] = tuple(
            sorted(
                items,
                key=lambda item: (item[0].endswith("*"), -len(item[0])),
            )
        )
        self._exclude = exclude
        self._routes: tuple[tuple[Pattern[str], float | None], ...] = ()
        self._refused: tuple[Pattern[str], ...] = ()
        self._refused_paths: frozenset[str] = frozenset()
        self._app: Any = None
        self._base_sources: tuple[tuple[Any, bool], ...] = ()
        self._topology: tuple[tuple[int, bool, tuple[Any, ...]], ...] = ()

    def _is_refused(self, path: str) -> bool:
        """Return whether the app refuses to have this path cached.

        A write, and a read behind a security scheme. A hit is answered
        before the app is routed, so caching either one answers over the
        gate or hands back what was never a read.
        """
        return path in self._refused_paths or any(
            regex.fullmatch(path) for regex in self._refused
        )

    def read(
        self,
        app: Annotated[Any, Doc("The application to read the rules off.")],  # noqa: ANN401
        *,
        include_root_middleware: bool = False,
    ) -> None:
        """Read every route the app declares that asked to be cached.

        Raises:
            TypeError: If a marked route answers a method other than `GET`.
        """
        self._app = app
        self._base_sources = ((app, include_root_middleware),)
        self._refresh(self._base_sources)

    def refresh(self, *apps: Any) -> None:  # noqa: ANN401
        """Refresh policy metadata when a request exposes changed routes."""
        sources = _unique_policy_sources(
            (
                *self._base_sources,
                *((app, False) for app in apps if app is not None),
            )
        )
        topology = tuple(
            (id(app), include_root, _route_topology(app))
            for app, include_root in sources
        )
        if topology != self._topology:
            self._refresh(sources, topology=topology)

    def _refresh(
        self,
        sources: tuple[tuple[Any, bool], ...],
        *,
        topology: tuple[tuple[int, bool, tuple[Any, ...]], ...] | None = None,
    ) -> None:
        """Read and publish routes from one coherent topology snapshot."""
        found: list[tuple[Pattern[str], float | None]] = []
        refused: list[tuple[str, Pattern[str]]] = []
        for app, include_root in sources:
            app_found, app_refused = _marked_routes(
                app,
                tuple(pattern for pattern, _ in self._include),
                self._exclude,
                include_root_middleware=include_root,
            )
            found.extend(app_found)
            refused.extend(app_refused)
        self._routes = tuple(found)
        # A route declared with no parameter answers one path, so it is
        # a set lookup. Only a template standing for many needs its
        # regex asked, and an app has few of those beside its literals.
        self._refused_paths = frozenset(
            template for template, _ in refused if "{" not in template
        )
        self._refused = tuple(
            regex for template, regex in refused if "{" in template
        )
        self._topology = (
            topology
            if topology is not None
            else tuple(
                (id(app), include_root, _route_topology(app))
                for app, include_root in sources
            )
        )

    def reread(self) -> None:
        """Read the app again, for the routes added since install."""
        if self._base_sources:
            self._refresh(self._base_sources)

    def pattern_ttl(
        self,
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[float, Doc("The component's own TTL.")],
    ) -> float | None:
        """Return how long `include` keeps this path, or `None` for never.

        The patterns only. A route's own declaration is read off the
        route, which a report walking the app has in hand and a request
        does not.

        A path the app refuses to have cached is answered `None`. The
        refusal is checked where the answer is given rather than only
        where a pattern is written, because a pattern names a URL and a
        route stands for many, so no reading of the patterns alone can
        be trusted to have seen them all.

        It is checked last, once a pattern would otherwise have said
        yes. The refused set holds every gated read the app declares,
        which on an authenticated API is most of them, and a request
        that no pattern names is not about to be cached anyway.
        """
        for pattern, ttl in self._include:
            if matches(path, (pattern,)):
                return (
                    None
                    if self._is_refused(path)
                    else (default if ttl is None else ttl)
                )
        return None

    def ttl_for(
        self,
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[float, Doc("The component's own TTL.")],
    ) -> float | None:
        """Return how long this path is cached, or `None` when it is not.

        A route that declared one says more than a pattern naming it, so
        `include=` fills in for the routes that declared none.

        The refusal is asked only once something would otherwise be
        kept, so a request nothing names costs no scan of it.
        """
        for regex, marked in self._routes:
            if regex.fullmatch(path):
                return (
                    None
                    if self._is_refused(path)
                    else (default if marked is None else marked)
                )
        return self.pattern_ttl(path, default)


def declared_ttl(
    route: Annotated[Any, Doc("The route to read the declaration off.")],  # noqa: ANN401
    contexts: Annotated[
        tuple[Any, ...],
        Doc("What was declared above it, outermost first."),
    ],
    declared: Annotated[str, Doc("The full path the route sits under.")],
) -> tuple[bool, float | None]:
    """Return whether this route declares a TTL, and the seconds it names.

    The nearest declaration decides: the route's own beats the router it
    sits in, and an inner router beats the one that includes it. `None`
    seconds means the declaration named none, so the component's own TTL
    applies.

    Read off the route rather than matched against its path, because a
    path is compiled before it matches anything: a route declared as
    `/products/{pid:int}` is a pattern, not a URL, and matching one
    against the other answers `no` for every typed converter.

    A declaration a route inherited from the router that holds it counts
    only where the cache may answer for it. A router holds more than
    reads, so the write under it, and a read with another dependency, are
    left to their handlers, and this says so too. A security scheme
    declared on the route itself is refused outright at install.
    """
    ttl = _declared_ttl(route, _declaring_above(contexts))
    for context in reversed(contexts):
        if ttl is not _UNMARKED:
            break
        ttl = _inherited_ttl(context)
    if ttl is _UNMARKED:
        return False, None
    if _unreadable(
        route, contexts, declared
    ) is not None or _has_non_cache_dependencies(route, contexts):
        return False, None
    return True, ttl


def _marked_routes(
    app: Any,  # noqa: ANN401
    named: tuple[str, ...] = (),
    excluded: tuple[str, ...] = (),
    *,
    include_root_middleware: bool = False,
) -> tuple[
    list[tuple[Pattern[str], float | None]], list[tuple[str, Pattern[str]]]
]:
    """Return the routes that declared a TTL, and the ones refused.

    The second list is every route a cache must not answer for, whether
    a pattern named it or not, so the answer at request time does not
    depend on how the path was written.

    Walks what the app declares, mounts and included routers alike, and
    compiles each full path with the framework's own compiler, off the
    path the route was written with, so a converter such as `{rest:path}`
    matches exactly what the router matches it with.

    Raises:
        TypeError: If a route that declared one answers a method other
            than `GET`, or gates itself behind a security scheme the
            cache would answer over. A path pattern naming a gated read
            is refused the same way, because a hit answers over the gate
            whichever of the two put the path here.
    """
    from starlette.routing import compile_path  # noqa: PLC0415

    found: list[tuple[Pattern[str], float | None]] = []
    refused = [
        *_middleware_refusals(app, include_root=include_root_middleware),
        *_authentication_refusals(app, include_root=include_root_middleware),
    ]
    answered: list[tuple[str, Pattern[str], frozenset[str]]] = []
    for prefix, route, contexts in walk_routes(app):
        above = _declaring_above(contexts)
        ttl = _declared_ttl(route, above)
        on_the_route = ttl is not _UNMARKED
        ttl = _inherited(ttl, contexts)
        declared = f"{prefix}{route.path}"
        compiled, _, _ = compile_path(declared)
        # Against the URL as well as the template. A route declared
        # `/users/{uid}` answers `/users/me`, so a pattern naming that
        # URL matches no template at all, and reading the template alone
        # would let it put a gated read in the cache.
        is_named, named_exactly = _named_by(named, declared, compiled)
        if is_named and _named_by(excluded, declared, compiled)[0]:
            # Carved out again, so no pattern is asking for this one.
            # `exclude` wins over `include` everywhere else, and a
            # refusal that ignored it would leave a prefix naming one
            # gated read with no way to keep the rest.
            is_named = named_exactly = False
        refusal = _unreadable(route, contexts, declared)
        if _dependency_bearing_read(
            route, contexts
        ) or _nested_get_has_dependencies(route):
            # Kept whether a pattern named it or not, so the answer at
            # request time does not depend on how it was named. Only a
            # dependency-bearing read: a write is already passed through
            # by the method guard, and one route's method must not speak
            # for another declared on the same path.
            refused.append((declared, compiled))
        if named_exactly:
            answered.append((declared, compiled, _methods_of(route)))
        if ttl is _UNMARKED:
            if is_named:
                _refuse_named_gate(route, contexts, declared)
            continue
        if refusal is not None:
            if on_the_route:
                raise TypeError(refusal)
            if is_named:
                # A router declares it for what it holds and holds more
                # than reads, so an inherited declaration is left alone.
                # A pattern naming this route is not inherited: somebody
                # wrote this path, and this path cannot be cached.
                _refuse_named_gate(route, contexts, declared)
            # What cannot be answered from a cache is left to its handler
            # rather than refused.
            continue
        found.append((compiled, cast("float | None", ttl)))
    _refuse_named_write(named, answered)
    return found, refused


def _middleware_refusals(
    app: Any,  # noqa: ANN401
    *,
    include_root: bool = False,
) -> list[tuple[str, Pattern[str]]]:
    """Compile exact and descendant refusals for every middleware boundary."""
    from starlette.routing import compile_path  # noqa: PLC0415

    found: list[tuple[str, Pattern[str]]] = []
    for boundary, nested in _middleware_boundaries(
        app, include_root=include_root
    ):
        exact = boundary or "/"
        exact_pattern, _, _ = compile_path(exact)
        found.append((exact, exact_pattern))
        if not nested:
            continue
        descendants = (
            f"{boundary.rstrip('/')}/{{path:path}}"
            if boundary
            else "/{path:path}"
        )
        descendant_pattern, _, _ = compile_path(descendants)
        found.append((descendants, descendant_pattern))
    return found


def _authentication_refusals(
    app: Any,  # noqa: ANN401
    *,
    include_root: bool,
) -> list[tuple[str, Pattern[str]]]:
    """Compile exact and descendant refusals for authentication boundaries."""
    from starlette.routing import compile_path  # noqa: PLC0415

    found: list[tuple[str, Pattern[str]]] = []
    for boundary, nested in _authentication_paths(app):
        if not include_root and not boundary:
            continue
        exact = boundary or "/"
        exact_pattern, _, _ = compile_path(exact)
        found.append((exact, exact_pattern))
        if not nested:
            continue
        descendants = (
            f"{boundary.rstrip('/')}/{{path:path}}"
            if boundary
            else "/{path:path}"
        )
        descendant_pattern, _, _ = compile_path(descendants)
        found.append((descendants, descendant_pattern))
    return found


def _nested_get_has_dependencies(route: Any) -> bool:  # noqa: ANN401
    """Return whether a leaf routing endpoint gates any possible GET."""
    nested = _nested_routing_app(route)
    return nested is not None and _routing_dependencies(nested, "GET")


def _routing_dependencies(
    app: Any,  # noqa: ANN401
    method: str,
    ancestors: frozenset[int] = frozenset(),
) -> bool:
    """Find a dependency below a routing endpoint without composing paths."""
    routed = _routing_app(app)
    if routed is None or id(routed) in ancestors:
        return False
    nested_ancestors = ancestors | {id(routed)}
    for _prefix, route, contexts in walk_routes(routed, unwrap_middleware=True):
        methods = {
            candidate.upper()
            for candidate in (getattr(route, "methods", None) or ())
        }
        if method in methods and _has_non_cache_dependencies(route, contexts):
            return True
        nested = _nested_routing_app(route)
        if nested is not None and _routing_dependencies(
            nested, method, nested_ancestors
        ):
            return True
    return False


def _inherited(ttl: object, contexts: tuple[Any, ...]) -> object:
    """Return the route's own declaration, or the nearest one above it.

    The nearest wins, so a router beats the one that includes it.
    """
    if ttl is not _UNMARKED:
        return ttl
    for context in reversed(contexts):
        found = _inherited_ttl(context)
        if found is not _UNMARKED:
            return found
    return _UNMARKED


def _named_by(
    named: tuple[str, ...],
    declared: str,
    compiled: Pattern[str],
) -> tuple[bool, bool]:
    """Return whether a pattern names this route, and whether one is exact.

    Exact means written for this path rather than a prefix that happens
    to cover it. A prefix names a router, and a router holds more than
    reads, so what it cannot cache is left to its handler. A path
    written out is somebody saying they want this one cached.
    """
    hits = [
        pattern for pattern in named if names_route(pattern, declared, compiled)
    ]
    return bool(hits), any(not pattern.endswith(_PREFIX) for pattern in hits)


def _dependency_bearing_read(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...],
) -> bool:
    """Return whether a GET runs a dependency other than the cache marker."""
    methods = {
        method.upper() for method in (getattr(route, "methods", None) or ())
    }
    return "GET" in methods and _has_non_cache_dependencies(route, contexts)


def _has_non_cache_dependencies(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...],
) -> bool:
    """Return whether FastAPI resolves anything besides the cache marker."""
    declared = getattr(route, "dependant", None)  # codespell:ignore
    route_provider = getattr(route, "dependency_overrides_provider", None)
    pending = [
        (dependency, route_provider)
        for dependency in getattr(declared, "dependencies", ()) or ()
    ]
    seen: set[int] = set()
    while pending:
        dependency, provider = pending.pop()
        if id(dependency) in seen:
            continue
        seen.add(id(dependency))
        call, effective, dependency_provider = _effective_dependency_call(
            dependency, provider
        )
        if (
            getattr(call, _MARKER, _UNMARKED) is _UNMARKED
            or effective is not call
        ):
            return True
        pending.extend(
            (child, dependency_provider)
            for child in getattr(dependency, "dependencies", ()) or ()
        )
    for context in contexts:
        context_provider = getattr(
            context, "dependency_overrides_provider", None
        )
        for dependency in getattr(context, "dependencies", ()) or ():
            call, effective, _ = _effective_dependency_call(
                dependency, context_provider or route_provider
            )
            if (
                getattr(call, _MARKER, _UNMARKED) is _UNMARKED
                or effective is not call
            ):
                return True
    return False


def _methods_of(route: Any) -> frozenset[str]:  # noqa: ANN401
    """Return the methods this route answers, upper case."""
    return frozenset(
        method.upper() for method in (getattr(route, "methods", None) or ())
    )


def _answered_by(
    pattern: str,
    answered: list[tuple[str, Pattern[str], frozenset[str]]],
) -> set[str]:
    """Return every method the routes this pattern names answer.

    The union across the path, because a path declares several routes
    and a write beside a read says nothing about the read.
    """
    found: set[str] = set()
    for declared, compiled, answers in answered:
        if names_route(pattern, declared, compiled):
            found |= answers
    return found


def _refuse_named_write(
    named: tuple[str, ...],
    answered: list[tuple[str, Pattern[str], frozenset[str]]],
) -> None:
    """Refuse a pattern written for a path that answers no read.

    Asked of the path rather than of one route, because a path declares
    several and a write beside a read says nothing about the read. Only
    a path written out, never a prefix: a prefix names a router, and a
    router holds writes beside its reads, which are simply left to their
    handlers. A path written out that answers no read is a typo, and it
    would otherwise cache nothing while reading as though it did.

    Raises:
        TypeError: If a pattern names only routes that answer no `GET`.
    """
    for pattern in named:
        if pattern.endswith(_PREFIX):
            continue
        methods = _answered_by(pattern, answered)
        if not methods or "GET" in methods:
            continue
        listed = ", ".join(sorted(methods))
        msg = (
            f"include= names {pattern!r}, which answers {listed} and no "
            f"GET. Only a read is cached, so this pattern would cache "
            f"nothing. Name a path that answers a GET, or leave it out."
        )
        raise TypeError(msg)


def _refuse_named_gate(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...],
    declared: str,
) -> None:
    """Refuse a pattern that names a read the caller has to be let past.

    Raises:
        TypeError: If the route is gated by a security scheme.
    """
    methods = {
        method.upper() for method in (getattr(route, "methods", None) or ())
    }
    if "GET" not in methods:
        return
    schemes = _gating_schemes(route, contexts)
    if not schemes:
        return
    named = ", ".join(sorted(set(schemes)))
    msg = (
        f"include= names {declared!r}, which is gated by {named}. A hit "
        "is answered before the app is routed, so the gate would not "
        "run, and one caller's response would be handed to whoever asks "
        "next. Name a path that answers everybody the same."
    )
    raise TypeError(msg)


def _gating_schemes(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...],
) -> list[str]:
    """Return every security scheme standing in front of this route."""
    gates = getattr(route, "dependant", None)  # codespell:ignore
    schemes = _security_schemes(gates)
    for context in contexts:
        schemes.extend(_declared_schemes(context))
    return schemes


def _unreadable(
    route: Any,  # noqa: ANN401
    contexts: tuple[Any, ...],
    declared: str,
) -> str | None:
    """Return why a response cache must not answer for this route.

    `None` says it may. A route that answers anything but a read, and one
    gated by a security scheme, are the two it must not: a hit is answered
    before the app is routed, so a gate declared on the route or on the
    router that holds it would never run, and one caller's response would
    go to whoever asks next.
    """
    methods = {
        method.upper() for method in (getattr(route, "methods", None) or ())
    } - {"HEAD", "OPTIONS"}
    if methods != {"GET"}:
        listed = ", ".join(sorted(methods)) or "no method"
        return (
            f"CachedResponse() is declared on {declared!r}, which answers "
            f"{listed}. A response cache answers a read, and a method that "
            "changes something must reach the handler every time. Declare "
            "it on the GET route instead."
        )
    schemes = _gating_schemes(route, contexts)
    if not schemes:
        return None
    named = ", ".join(sorted(set(schemes)))
    return (
        f"CachedResponse() is declared on {declared!r}, which is gated by "
        f"{named}. A hit is answered before the app is routed, so the gate "
        "would not run, and one caller's response would be handed to "
        "whoever asks next. Cache a route that answers everybody the same."
    )


def _declared_schemes(context: Any) -> list[str]:  # noqa: ANN401
    """Return the security schemes an included router gates everything with.

    An include's dependencies are held as they were written rather than
    resolved into each route, so each one is resolved here the way the
    framework resolves it, and a scheme a dependency of its own declares
    counts as much as one written on the include.
    """
    try:
        from fastapi.dependencies.utils import get_dependant  # noqa: PLC0415
        from fastapi.security.base import SecurityBase  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the reimport test walks this
        return []
    found: list[str] = []
    for dependency in getattr(context, "dependencies", ()) or ():
        call = getattr(dependency, "dependency", None)
        if call is None:
            continue
        if isinstance(call, SecurityBase):
            found.append(type(call).__name__)
            continue
        found.extend(_security_schemes(get_dependant(path="/", call=call)))
    return found


def _security_schemes(gates: Any) -> list[str]:  # noqa: ANN401
    """Return the security schemes a route is gated by, by name.

    Walks the whole dependency tree, because a scheme declared inside a
    dependency of a dependency gates the route just as much as one
    written on it.
    """
    try:
        from fastapi.security.base import SecurityBase  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the reimport test walks this
        return []
    found: list[str] = []
    pending = list(getattr(gates, "dependencies", ()))
    while pending:
        dependency = pending.pop()
        if isinstance(dependency.call, SecurityBase):
            found.append(type(dependency.call).__name__)
        pending.extend(getattr(dependency, "dependencies", ()))
    return found


def _declared_ttl(route: Any, above: set[int]) -> Any:  # noqa: ANN401
    """Return the TTL this route declared, or `_UNMARKED` for one that did not.

    Read off the resolved dependency tree, under the framework's own
    spelling of it. A framework that resolves none declares nothing here,
    and names its paths in `include=` instead.

    What a router declared through its own constructor is resolved into
    every route it holds, so `above` says which of them the route did not
    write itself.
    """
    declared = getattr(route, "dependant", None)  # codespell:ignore
    for dependency in getattr(declared, "dependencies", ()):
        ttl = getattr(dependency.call, _MARKER, _UNMARKED)
        if ttl is not _UNMARKED and id(dependency.call) not in above:
            return ttl
    return _UNMARKED


def _declaring_above(contexts: tuple[Any, ...]) -> set[int]:
    """Return what the routers above this route declared, by identity.

    A declaration is a callable of its own, so the one a router was built
    with is the same object in every route it holds, and telling it from
    one written on the route is a matter of which object it is.
    """
    return {
        id(marked)
        for context in contexts
        for dependency in getattr(context, "dependencies", ()) or ()
        if getattr(
            marked := getattr(dependency, "dependency", None),
            _MARKER,
            _UNMARKED,
        )
        is not _UNMARKED
    }


def _inherited_ttl(context: Any) -> Any:  # noqa: ANN401
    """Return the TTL an included router declares for everything under it."""
    for dependency in getattr(context, "dependencies", ()) or ():
        ttl = getattr(
            getattr(dependency, "dependency", None), _MARKER, _UNMARKED
        )
        if ttl is not _UNMARKED:
            return ttl
    return _UNMARKED


class CachedResponsesConfig(BaseModel, frozen=True, extra="forbid"):
    """Cached Responses Config.

    `include` is the one field in the HTTP family whose patterns carry a
    value, because the cache is the one rule with something to say per
    path. A tuple names the paths at the component's own `ttl`, and a
    mapping gives each its own seconds.
    """

    ttl: Annotated[
        float,
        AfterValidator(_seconds),
        Doc("Seconds a response is kept when its route names none."),
    ] = _DEFAULT_TTL
    include: Annotated[
        PathPatterns | Mapping[str, float],
        AfterValidator(_seconds_per_path),
        Doc(
            "The paths cached. A tuple keeps each for `ttl`, and a "
            'mapping such as `{"/products/*": 60}` gives each its own '
            "seconds. The most specific pattern decides."
        ),
    ] = ()
    exclude: Annotated[
        PathPatterns,
        Doc("Paths never cached, whatever a route or `include` says."),
    ] = ()
    vary_by_headers: Annotated[
        FieldNames,
        Doc(
            "Request headers whose value is part of the key. A response "
            "whose `Vary` names a header outside this set is not stored."
        ),
    ] = ()
    vary_by_query: Annotated[
        FieldNames | None,
        Doc(
            "Query parameters that are part of the key. `None` keys on "
            "the whole query string."
        ),
    ] = None
    max_body_size: Annotated[
        PositiveInt,
        Doc("Largest response body stored, in bytes."),
    ] = _DEFAULT_MAX_BODY_SIZE


@dataclass(frozen=True, slots=True)
class _State:
    """What the middleware answers one request from.

    Holds the configuration beside the values derived from it. A cache
    is the one middleware where reading two of these from different
    snapshots could answer one caller with another's response, so they
    are taken together or not at all.
    """

    config: CachedResponsesConfig
    policies: _Policies
    vary_by_headers: tuple[str, ...]


def _state_of(config: CachedResponsesConfig, policies: _Policies) -> _State:
    """Derive what the request path needs from a configuration.

    Header names are folded to lower case once here, because a scope
    carries them lower case and a caller may not have.
    """
    return _State(
        config=config,
        policies=policies,
        vary_by_headers=tuple(name.lower() for name in config.vary_by_headers),
    )


class CachedResponsesMiddleware:
    """Answer a repeated read from the cache instead of the handler.

    A `GET` or `HEAD` whose path a rule names is looked up first. A hit is
    answered without reaching the app, carrying the `Age` it has spent in
    the cache, and a client that already holds the entity tag is answered
    `304 Not Modified` with no body at all.

    ```python
    from grelmicro.cache import TTLCache
    from grelmicro.http import CachedResponsesMiddleware

    app.add_middleware(
        CachedResponsesMiddleware,
        cache=TTLCache(),
        include={"/products/*": 60},
    )
    ```

    Register `CachedResponses()` instead to have `micro.install(app)` add
    it for you, and to declare `CachedResponse(ttl=...)` on a route.

    A miss runs the handler once. Every other request for the same key
    waits for that one and is answered from what it stored, in process and
    across replicas, so a cold key never fans one computation out to every
    caller at once.

    A request asking for a range is passed through, because what is
    stored is the whole resource.

    A response is stored only when it is safe to hand to somebody else:
    status `200`, no `Set-Cookie`, no `Content-Encoding`, no
    `Cache-Control` refusing it, and a `Vary` naming nothing outside
    `vary_by_headers`. A request carrying `Authorization` or `Cookie`
    never reads the cache and never fills it. Neither does one an outer
    ASGI authentication middleware has already marked as authenticated.

    A request's own `Cache-Control` is not read. This answers for the
    resource rather than for one caller, so a caller that could ask for
    the handler could spend it at will.

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
        cache: Annotated[
            TTLCache[Any],
            Doc("The `TTLCache` responses are stored in."),
        ],
        policies: Annotated[
            _Policies | None,
            Doc(
                "The paths declaring `CachedResponse()`, filled by "
                "`micro.install(app)`. `include` alone needs none."
            ),
        ] = None,
        ttl: Annotated[
            float,
            Doc("Seconds a response is kept when its route names none."),
        ] = _DEFAULT_TTL,
        include: Annotated[
            Mapping[str, float] | tuple[str, ...] | None,
            Doc(
                "The paths cached. A tuple keeps each for `ttl`, and a "
                "mapping gives each its own seconds. Exact match unless "
                "the pattern ends with `*`."
            ),
        ] = None,
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths this middleware leaves alone, whatever else says. "
                "Same matching."
            ),
        ] = (),
        vary_by_headers: Annotated[
            tuple[str, ...],
            Doc(
                "Request headers whose value is part of the key, so one "
                "value never answers another. A response whose `Vary` "
                "names a header outside this set is not stored."
            ),
        ] = (),
        vary_by_query: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Query parameters that are part of the key. `None` (the "
                "default) keys on the whole query string."
            ),
        ] = None,
        key: Annotated[
            Callable[[Scope], str | None] | None,
            Doc(
                "Builds the key from the ASGI scope, replacing the path "
                "and the vary rules. Return `None` to leave a request "
                "uncached."
            ),
        ] = None,
        skip: Annotated[
            Callable[[StoredResponse], bool] | None,
            Doc("Returns whether one response is left unstored."),
        ] = None,
        max_body_size: Annotated[
            int,
            Doc(
                "Largest response body stored, in bytes. A larger one is "
                "streamed to the client and not kept."
            ),
        ] = _DEFAULT_MAX_BODY_SIZE,
        tag: Annotated[
            str,
            Doc("Tag every entry carries, so `purge()` deletes them all."),
        ] = "grelmicro:http:default",
        live: Annotated[
            Live[_State] | None,
            Doc(
                "The cell a registered `CachedResponses` publishes its "
                "snapshot into, filled by `micro.install(app)`. Passing it "
                "makes the other options the component's to decide."
            ),
        ] = None,
    ) -> None:
        """Initialize the middleware with the paths it answers for.

        Raises:
            TypeError: If a set of path patterns was given as a string.
        """
        self.app = app
        # Refused before the configuration is built, so hand-wired ASGI
        # gets the argument error its layer speaks rather than pydantic's
        # report that a string is not a mapping.
        if isinstance(include, str):
            raise TypeError(BARE_STRING_MESSAGE)
        self._cache = cache
        self._key = key
        self._skip = skip
        self._tag = tag
        # A middleware built by hand owns its cell and never sees a new
        # snapshot, so the two doors read exactly the same way.
        if live is not None:
            self._live = live
        else:
            config = build_config(
                CachedResponsesConfig,
                ttl=ttl,
                include=include or (),
                exclude=as_patterns(exclude, name="exclude"),
                vary_by_headers=as_patterns(
                    vary_by_headers, name="vary_by_headers"
                ),
                vary_by_query=(
                    None
                    if vary_by_query is None
                    else as_patterns(vary_by_query, name="vary_by_query")
                ),
                max_body_size=max_body_size,
            )
            owned_policies = (
                policies
                if policies is not None
                else _Policies(
                    include or {}, as_patterns(exclude, name="exclude")
                )
            )
            if _is_starlette_routing_app(app):
                # A bound Router.app is the entry point below that Router's
                # own stack. Its current cache and any outer middleware do
                # not run below this instance and therefore are not
                # boundaries; route middleware still is.
                owned_policies.read(
                    app,
                    include_root_middleware=_bound_router(app) is None,
                )
            self._live = Live(_state_of(config, owned_policies))
        self._warned: set[str] = set()
        self._reported: dict[str, float] = {}
        self._unstorable: OrderedDict[str, None] = OrderedDict()

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Answer from the cache, or run the app once and keep what it said."""
        if scope["type"] != "http" or scope["method"] not in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        # One read, at the top, for the whole request. A cache is the one
        # middleware where two fields from two snapshots could answer one
        # caller with another's response, so they are taken together.
        state = self._live.state
        config = state.config
        path = route_path(scope)
        if matches(path, config.exclude):
            await self.app(scope, receive, send)
            return
        if _carries_credentials(scope) or _asks_for_part(scope):
            await self.app(scope, receive, send)
            return
        state.policies.refresh(scope.get("app"))
        ttl = state.policies.ttl_for(path, config.ttl)
        if ttl is None:
            await self.app(scope, receive, send)
            return
        built = (
            self._key(scope)
            if self._key is not None
            else self._built(state, scope, path)
        )
        if built is None:
            await self.app(scope, receive, send)
            return
        await self._answer(
            scope, receive, send, state=state, key=built, ttl=ttl, path=path
        )

    async def _answer(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        state: _State,
        key: str,
        ttl: float,
        path: str,
    ) -> None:
        """Serve the stored response, or run the app and store what it said.

        A `HEAD` reads the entry a `GET` stored and fills none of its own,
        because its body is empty and would answer the `GET` that follows
        it with nothing.
        """
        storage_key = self._storage_key(key)
        entry = await self._read(storage_key)
        if entry is not None:
            await _serve(entry, scope, send)
            return
        capture = _ResponseCapture(
            send, max_body_size=state.config.max_body_size
        )
        if scope["method"] != "GET":
            # A `HEAD` reads what a `GET` stored and fills nothing, so
            # folding it would hold the key a `GET` is waiting for while
            # producing a response nobody keeps.
            await self.app(scope, receive, capture)
            await capture.flush()
            await capture.release(complete=capture.complete)
            return
        attempt = _Attempt()

        async def compute() -> _Entry:
            attempt.ran = True
            await self.app(scope, receive, capture)
            await capture.flush()
            attempt.entry = self._entry_of(capture, state=state, path=path)
            attempt.returned = True
            if attempt.entry is None:
                raise _NotStored
            await self._write(
                storage_key,
                attempt.entry,
                ttl=min(ttl, attempt.entry["kept"]),
            )
            return attempt.entry

        try:
            entry = await self._folded(storage_key, compute, attempt)
        except _NotStored:
            self._remember_unstorable(storage_key)
            await capture.release(complete=capture.complete)
            return
        self._unstorable.pop(storage_key, None)
        await _serve(entry, scope, send)

    async def _folded(
        self,
        storage_key: str,
        compute: Callable[[], Awaitable[_Entry]],
        attempt: _Attempt,
    ) -> _Entry:
        """Run `compute` once for this key, however the store behaves.

        A key nothing is ever stored under takes no lock, so a stream
        does not queue every caller behind the one in front of it. A
        store that cannot be reached takes none either: the read still
        has to be answered, and the handler is what answers it.

        A lock that fails on its way out arrives here after the handler
        has already answered. What it answered is what the caller gets,
        because the request succeeded and only the bookkeeping did not.

        Raises:
            _NotStored: If the response is not one to keep.
        """
        if storage_key in self._unstorable:
            return await compute()
        try:
            return cast(
                "_Entry",
                await compute_with_stampede(
                    self._cache,
                    storage_key,
                    compute,
                    self._cache._stampede,  # noqa: SLF001
                    per_key=True,
                    auto_distributed=True,
                ),
            )
        except _NotStored:
            raise
        except Exception as error:
            if not attempt.ran:
                self._report(
                    error,
                    "fold",
                    "response cache could not fold this read, running the "
                    "handler",
                )
                return await compute()
            if not attempt.returned:
                raise
            self._report(
                error,
                "release",
                "response cache lost hold of this read after the handler "
                "answered it",
            )
            if attempt.entry is None:
                raise _NotStored from None
            return attempt.entry

    async def _read(self, storage_key: str) -> _Entry | None:
        """Return the stored response, or nothing when the store cannot say.

        A cache that cannot be reached is a cache miss. Failing the
        request instead would make every path named here less available
        than it was before it was cached.
        """
        try:
            return cast("_Entry | None", await self._cache.get(storage_key))
        except Exception as error:  # noqa: BLE001
            self._report(
                error,
                "read",
                "response cache could not be read, answering from the handler",
            )
            return None

    async def _write(
        self, storage_key: str, entry: _Entry, *, ttl: float
    ) -> None:
        """Keep the response, and let the caller have it either way."""
        try:
            await self._cache.set(storage_key, entry, ttl, tags=(self._tag,))
        except Exception as error:  # noqa: BLE001
            self._report(
                error,
                "write",
                "response cache kept nothing, the response still went out",
            )

    def _report(self, error: BaseException, reason: str, message: str) -> None:
        """Say the store failed, at most once a minute for each reason.

        A store that is down is one every request goes past, and a
        traceback per request is the last thing an outage needs.
        """
        now = clock_time()
        if now - self._reported.get(reason, -math.inf) < _REPORT_INTERVAL:
            logger.debug(message)
            return
        self._reported[reason] = now
        logger.warning(message, exc_info=error)

    def _remember_unstorable(self, storage_key: str) -> None:
        """Note a key nothing was stored under, so it stops taking the lock.

        A path whose responses are never storable, a stream above all,
        would otherwise queue every caller behind the one in front of it,
        and behind a cross-replica lock when one is configured.
        """
        self._unstorable[storage_key] = None
        while len(self._unstorable) > _UNSTORABLE_LIMIT:
            self._unstorable.popitem(last=False)

    def _built(self, state: _State, scope: Scope, path: str) -> str:
        """Return the key this request reads.

        The scheme, the host, and the prefix the app is served under are
        part of it, so an app answering for two hostnames, and two
        services behind one gateway sharing one store, never hand out
        each other's responses.
        """
        parts = [
            scope.get("scheme", "http"),
            _header_of(scope, "host"),
            scope.get("root_path", ""),
            path,
            _query_of(scope, state.config.vary_by_query),
        ]
        parts.extend(_header_of(scope, name) for name in state.vary_by_headers)
        return "\x00".join(parts)

    def _storage_key(self, key: str) -> str:
        """Return the cache key one request's key is stored under."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self._tag}:{digest}"

    def _entry_of(
        self, capture: _ResponseCapture, *, state: _State, path: str
    ) -> _Entry | None:
        """Return the entry this response is stored as, or `None` to skip it."""
        start = capture.start
        if start is None or capture.released or not capture.complete:
            return None
        if start["status"] != _HTTP_200_OK:
            return None
        headers = list(start["headers"])
        kept = self._storable(headers, state=state, path=path)
        if kept is None:
            return None
        body = capture.body
        named = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in headers
        }
        if self._skip is not None and self._skip(
            StoredResponse(
                status=start["status"],
                headers=named,
                body=body,
            )
        ):
            return None
        if "etag" not in named:
            tag = etag_of(body)
            headers.append((b"etag", tag.encode("latin-1")))
        headers = self._declaring_vary(state, headers)
        return _Entry(
            kept=kept,
            status=start["status"],
            headers=[
                [name.decode("latin-1"), value.decode("latin-1")]
                for name, value in headers
            ],
            body=body.decode("latin-1"),
            stored_at=clock_time(),
        )

    def _declaring_vary(
        self, state: _State, headers: list[tuple[bytes, bytes]]
    ) -> list[tuple[bytes, bytes]]:
        """Return the headers with what the key reads named in `Vary`.

        The middleware keys on these, so the response does vary on them,
        and everything downstream has to be told: a CDN, a proxy, or the
        browser's own cache would otherwise hand one caller's copy to the
        next one who sent a different value.
        """
        if not state.vary_by_headers:
            return headers
        kept = [
            (name, value) for name, value in headers if name.lower() != b"vary"
        ]
        named = dict.fromkeys(
            [
                part
                for name, value in headers
                if name.lower() == b"vary"
                for part in _split_field(value)
            ]
            + list(state.vary_by_headers)
        )
        kept.append((b"vary", ", ".join(named).encode("latin-1")))
        return kept

    def _storable(
        self,
        headers: Sequence[tuple[bytes, bytes]],
        *,
        state: _State,
        path: str,
    ) -> float | None:
        """Return the longest this response may be kept, or `None` for never.

        Every occurrence of a header counts. A response carrying two
        `Cache-Control` lines, or two `Vary` lines, says all of what they
        say, and reading only the last of them is how the one that
        refused the store goes missing.

        A response naming its own freshness is kept no longer than it
        says, and one that says it is already stale is not kept at all.
        """
        directives: set[str] = set()
        varies: list[str] = []
        for name, value in headers:
            lowered = name.lower()
            if lowered in _UNCACHEABLE_RESPONSE_HEADERS:
                return None
            if lowered == b"cache-control":
                directives.update(_split_field(value))
            elif lowered == b"vary":
                varies.extend(_split_field(value))
        if directives & _UNCACHEABLE_DIRECTIVES:
            return None
        if varies:
            vary = ", ".join(varies)
            if "*" in varies or set(varies) - set(state.vary_by_headers):
                self._warn(path, vary)
                return None
        return _named_freshness(directives)

    def _warn(self, path: str, vary: str) -> None:
        """Say once that a response was not stored because of its `Vary`."""
        if path in self._warned:
            return
        if len(self._warned) >= _WARNED_LIMIT:
            self._warned.clear()
        self._warned.add(path)
        logger.warning(
            "response cache kept nothing for %s: its Vary is %r, and a "
            "header outside vary_by_headers would answer one client with "
            "another one's response",
            path,
            vary,
        )


class _Attempt:
    """What one run of the app under the fold got as far as.

    Read when the fold itself fails, to tell a handler that never ran
    from one that answered and only lost the lock on its way out.
    """

    __slots__ = ("entry", "ran", "returned")

    def __init__(self) -> None:
        """Start with a run that has not happened."""
        self.ran = False
        """Whether the app was called at all."""
        self.returned = False
        """Whether it returned, so what it said has been decided."""
        self.entry: _Entry | None = None
        """What it said, when that is something to keep."""


class _NotStored(Exception):  # noqa: N818
    """Carries "this response is not kept" out of the folded computation.

    The response is already on its way to the caller by the time this
    travels, so it says what not to store rather than what went wrong.
    """


class _ResponseCapture:
    """Hold one response so it can be stored, or let it through when it cannot.

    A response is held only while holding it can still pay off, which is
    while the body arrives in one piece and stays under `max_body_size`. A
    streamed response, a larger one, and one that declares trailers are
    forwarded as they come and stored not at all.
    """

    def __init__(
        self,
        send: Send,
        *,
        max_body_size: int,
    ) -> None:
        """Initialize the capture around the downstream `send`."""
        self._send = send
        self._max_body_size = max_body_size
        self.start: Message | None = None
        self.released = False
        self.complete = False
        self._chunks: list[bytes] = []
        self._size = 0

    @property
    def body(self) -> bytes:
        """Return the body held so far."""
        return b"".join(self._chunks)

    async def __call__(self, message: Message) -> None:
        """Hold the response, or forward what can no longer be held."""
        if self.released:
            await self._send(message)
            return
        if message["type"] == "http.response.start":
            self.start = message
            if message.get("trailers"):
                await self.release()
            return
        if message["type"] != "http.response.body":
            await self.release(complete=self.complete)
            await self._send(message)
            return
        if message.get("more_body", False):
            await self.release()
            await self._send(message)
            return
        chunk = message.get("body", b"")
        self._size += len(chunk)
        if self._size > self._max_body_size:
            await self.release()
            await self._send(message)
            return
        self._chunks.append(chunk)
        self.complete = True

    async def flush(self) -> None:
        """Release a response the app stopped without finishing.

        A complete one is held for the entry to be built from, and goes
        out from there.
        """
        if not self.complete:
            await self.release(complete=True)

    async def release(self, *, complete: bool = False) -> None:
        """Send what is held, and stop holding.

        `complete` says the held chunks are the whole body, which is the
        one case where this closes the response rather than leaving it
        open for what the app is still sending.
        """
        if self.released:
            return
        self.released = True
        if self.start is None:
            return
        await self._send(self.start)
        if self._chunks or complete:
            await self._send(
                {
                    "type": "http.response.body",
                    "body": self.body,
                    "more_body": not complete,
                }
            )


async def _serve(entry: _Entry, scope: Scope, send: Send) -> None:
    """Answer from a stored response, or with the `304` it allows."""
    age = max(0, int(clock_time() - entry["stored_at"]))
    headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in entry["headers"]
    ]
    etag = next(
        (
            value.decode("latin-1")
            for name, value in headers
            if name.lower() == b"etag"
        ),
        None,
    )
    held = _tags(scope["headers"], b"if-none-match")
    if etag is not None and held is not None and _matches_weak(held, etag):
        kept = [
            (name, value)
            for name, value in headers
            if name.lower() in _KEPT_ON_304
        ]
        kept.append((b"age", str(age).encode("latin-1")))
        await send(
            {
                "type": "http.response.start",
                "status": _HTTP_304_NOT_MODIFIED,
                "headers": kept,
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    headers.append((b"age", str(age).encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": entry["status"],
            "headers": headers,
        }
    )
    body = b"" if scope["method"] == "HEAD" else entry["body"].encode("latin-1")
    await send({"type": "http.response.body", "body": body})


def _named_freshness(directives: set[str]) -> float | None:
    """Return the seconds a response says it stays fresh for.

    `s-maxage` is the one written for a shared cache, so it wins over
    `max-age` where both are named. `inf` says the response named
    neither, and `None` that it named one it has already spent.
    """
    kept = math.inf
    shared = math.inf
    for directive in directives:
        for name in ("s-maxage", "max-age"):
            if not directive.startswith(f"{name}="):
                continue
            try:
                seconds = float(directive.split("=", 1)[1])
            except ValueError:
                continue
            if name == "s-maxage":
                shared = min(shared, seconds)
            else:
                kept = min(kept, seconds)
    named = shared if shared != math.inf else kept
    return None if named <= 0 else named


def _asks_for_part(scope: Scope) -> bool:
    """Return whether the request asked for a range rather than the whole.

    A stored `200` is the whole resource, and answering a range with it
    would turn every partial read into a full download, quietly.
    """
    return any(
        name.lower() == _PARTIAL_REQUEST_HEADER for name, _ in scope["headers"]
    )


def _split_field(value: bytes) -> list[str]:
    """Return one comma-separated header value as its lowercased parts."""
    return [
        part.strip().lower()
        for part in value.decode("latin-1").split(",")
        if part.strip()
    ]


def _carries_credentials(scope: Scope) -> bool:
    """Return whether the request is one caller's, so no cache may answer it."""
    return _authenticated_scope(scope) or any(
        name.lower() in _PRIVATE_REQUEST_HEADERS for name, _ in scope["headers"]
    )


def _query_of(scope: Scope, selected: tuple[str, ...] | None) -> str:
    """Return the query string the key reads, in one order whatever order it came in."""
    raw = scope.get("query_string", b"").decode("latin-1")
    if not raw:
        return ""
    pairs = sorted(part for part in raw.split("&") if part)
    if selected is None:
        return "&".join(pairs)
    wanted = set(selected)
    return "&".join(pair for pair in pairs if pair.split("=", 1)[0] in wanted)


def _header_of(scope: Scope, name: str) -> str:
    """Return one request header's value, empty when it is absent."""
    wanted = name.encode("latin-1")
    return ",".join(
        value.decode("latin-1")
        for key, value in scope["headers"]
        if key.lower() == wanted
    )


class CachedResponses(Reconfigurable[CachedResponsesConfig]):
    """Serve repeated reads from the cache, wired by `micro.install(app)`.

    Register it, mark the routes it answers for, and `install` adds
    `CachedResponsesMiddleware` to the app:

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.cache import Cache
    from grelmicro.http import CachedResponses
    from grelmicro.integrations.fastapi import CachedResponse
    from grelmicro.providers.redis import RedisProvider

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[Cache(redis), CachedResponses()])
    app = FastAPI()
    micro.install(app)

    @app.get("/products", dependencies=[CachedResponse(ttl=60)])
    async def list_products() -> list[Product]: ...
    ```

    The bare form caches nothing until a route declares it. `include=` names
    URLs instead, for a router whose routes you cannot touch and for a
    framework that resolves no dependencies grelmicro can read.

    It rides the registered `Cache`, so a response one replica computed
    answers the callers of every other one. Pass a `TTLCache` of your own
    to store them somewhere else.

    Register it before `ConditionalRequests()`, so a hit is answered
    without entering it.

    Every option of `CachedResponsesMiddleware` is taken here and
    forwarded, so a registered component and a hand-added middleware
    answer the same.

    A framework that serves no HTTP, such as FastStream, ignores it.

    Read more in the [Response Cache](../http/cache.md) docs.
    """

    kind: ClassVar[str] = "cached_responses"

    def __init__(  # noqa: PLR0913
        self,
        *,
        ttl: Annotated[
            float | None,
            Doc(
                "Seconds a response is kept when its route names none. "
                "`CachedResponse(ttl=...)` overrides it per route."
            ),
        ] = None,
        include: Annotated[
            Mapping[str, float] | tuple[str, ...] | None,
            Doc(
                "The paths cached, for a route that declares none. A "
                "tuple keeps each for `ttl`, and a mapping gives each its "
                'own seconds, as `{"/products/*": 60}`. Exact match '
                "unless the pattern ends with `*`."
            ),
        ] = None,
        exclude: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Paths never cached, whatever a route or `include` says. "
                "Same matching."
            ),
        ] = None,
        vary_by_headers: Annotated[
            tuple[str, ...] | None,
            Doc(
                "Request headers whose value is part of the key. A "
                "response whose `Vary` names a header outside this set is "
                "not stored, because one value would answer another."
            ),
        ] = None,
        vary_by_query: Annotated[
            tuple[str, ...] | _Unset | None,
            Doc(
                "Query parameters that are part of the key. `None` (the "
                "default) keys on the whole query string, and passing it "
                "says so over any variable."
            ),
        ] = UNSET,
        key: Annotated[
            Callable[[Scope], str | None] | None,
            Doc(
                "Builds the key from the ASGI scope, replacing the path "
                "and the vary rules. Return `None` to leave a request "
                "uncached."
            ),
        ] = None,
        skip: Annotated[
            Callable[[StoredResponse], bool] | None,
            Doc("Returns whether one response is left unstored."),
        ] = None,
        max_body_size: Annotated[
            int | None,
            Doc("Largest response body stored, in bytes."),
        ] = None,
        cache: Annotated[
            TTLCache[Any] | None,
            Doc(
                "The `TTLCache` responses are stored in. Defaults to one "
                "over the registered `Cache`."
            ),
        ] = None,
        namespace: Annotated[
            str,
            Doc(
                "Namespace the stored keys sit under, so two sets of "
                "rules on one app never read each other's responses. Part "
                "of every stored key, so it is not live."
            ),
        ] = "http",
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
        env_prefix: Annotated[
            str | None,
            Doc(
                "Override the derived prefix, `GREL_CACHED_RESPONSES_` for "
                "the default instance."
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
        """Answer repeated reads through the registered middleware.

        Raises:
            SettingsValidationError: If `ttl`, or one a pattern names, is
                not a positive number of seconds.
        """
        resolved_env_prefix, kind_prefix = env_prefixes(
            "CACHED_RESPONSES", name, env_prefix
        )
        config = resolve_config(
            CachedResponsesConfig,
            explicit=None,
            kwargs={
                "ttl": ttl,
                "include": include,
                "exclude": exclude,
                "vary_by_headers": vary_by_headers,
                "vary_by_query": (
                    None if isinstance(vary_by_query, _Unset) else vary_by_query
                ),
                "max_body_size": max_body_size,
            },
            env_prefix=resolved_env_prefix,
            kind_env_prefix=kind_prefix,
            env_load=env_load,
        )
        if vary_by_query is None:
            # Written by the caller rather than left out, and a `None`
            # keyword reads as absent to `resolve_config`, so it is put
            # back over whatever a variable said.
            config = config.model_copy(update={"vary_by_query": None})
        self._setup(
            config,
            name=name,
            namespace=namespace,
            cache=cache,
            key=key,
            skip=skip,
        )
        self._track_reconfigure(resolved_env_prefix)

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            CachedResponsesConfig,
            Doc("The pre-built cached responses configuration."),
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
        cache: Annotated[
            TTLCache[Any] | None,
            Doc("The `TTLCache` responses are stored in."),
        ] = None,
        key: Annotated[
            Callable[[Scope], str | None] | None,
            Doc("Builds the key from the ASGI scope."),
        ] = None,
        skip: Annotated[
            Callable[[StoredResponse], bool] | None,
            Doc("Returns whether one response is left unstored."),
        ] = None,
    ) -> CachedResponses:
        """Build the component from a configuration that is already whole.

        The one declarative door. What you pass is what runs: no
        environment variable is read, and the instance is not registered
        for live reload. The store and the two callables stay here rather
        than in the config, because they are objects rather than values.
        """
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            config,
            name=name,
            namespace=namespace,
            cache=cache,
            key=key,
            skip=skip,
        )
        return instance

    def _setup(
        self,
        config: CachedResponsesConfig,
        *,
        name: str,
        namespace: str,
        cache: TTLCache[Any] | None,
        key: Callable[[Scope], str | None] | None,
        skip: Callable[[StoredResponse], bool] | None,
    ) -> None:
        """Hold the configuration, the store, and the middleware's cell."""
        self._name = name
        self._key = key
        self._skip = skip
        self._cache: TTLCache[Any] = (
            cache
            if cache is not None
            else TTLCache(
                ttl=config.ttl, name=name, serializer=JsonSerializer()
            )
        )
        self._tag = f"grelmicro:{namespace}:{name}"
        self._policies = _Policies(config.include, config.exclude)
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        self._live: Live[_State] = Live(_state_of(config, self._policies))

    async def _apply_reconfigure(
        self, new_config: CachedResponsesConfig
    ) -> None:
        """Publish the snapshot the next request reads.

        The new patterns are read against the app's own routes first, the
        same reading `micro.install(app)` does. A pattern naming a write
        or a read behind a security scheme is refused at install, and a
        read with another dependency is left uncached. Reload has to
        preserve both decisions: a mounted file must not be able to start
        caching what the static path would not.
        """
        policies = _Policies(new_config.include, new_config.exclude)
        app = self._policies._app  # noqa: SLF001
        if app is not None:
            policies.read(app)
        self._policies = policies
        self._live.state = _state_of(new_config, policies)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def cache(self) -> TTLCache[Any]:
        """Return the `TTLCache` the responses are stored in."""
        return self._cache

    async def purge(self) -> None:
        """Delete every response this component stored.

        What a write invalidates, so a handler that changed the resource
        drops what the cache would go on answering with until the TTL
        elapsed.
        """
        await self._cache.delete_tags(self._tag)

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with.

        The middleware is handed the cell rather than the values, so a
        live reconfigure reaches it without the stack being rebuilt,
        which a framework will not do once it is serving.
        """
        return CachedResponsesMiddleware, {
            "cache": self._cache,
            "key": self._key,
            "skip": self._skip,
            "tag": self._tag,
            "live": self._live,
        }

    def read_routes(
        self,
        app: Annotated[Any, Doc("The application to read the rules off.")],  # noqa: ANN401
    ) -> None:
        """Read `CachedResponse()` off every route the app declares.

        Called by the integration after the middleware is added. The app
        is read again when it starts, so a route added between the two
        counts as well.
        """
        self._policies.read(app)

    async def __aenter__(self) -> Self:
        """Open the component, reading the routes the app now declares."""
        self._policies.reread()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the component. Nothing to close."""
        return None
