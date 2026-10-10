"""Response cache for HTTP reads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict, abc

# Imported at runtime, not under `TYPE_CHECKING`: it appears in a config
# field's annotation, which pydantic resolves from module globals.
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
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
from urllib.parse import parse_qsl, unquote_plus

from pydantic import BaseModel, BeforeValidator, PositiveInt
from typing_extensions import Doc

from grelmicro._config import (
    Live,
    Reconfigurable,
    build_config,
    env_prefixes,
    resolve_config,
)
from grelmicro._duration import Duration, check_duration, read_duration
from grelmicro._paths import (
    _PREFIX,
    BARE_STRING_MESSAGE,
    FieldNames,
    PathPatterns,
    _request_authority,
    _request_root_path,
    _request_scheme,
    _RouteTopologyState,
    as_patterns,
    declared_dependencies,
    holds_control_character,
    matches,
    names_route,
    route_path,
)
from grelmicro.cache._stampede import (
    Fold,
    check_fold,
    compute_with_stampede,
)
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.http._conditional import (
    _KEPT_ON_304,
    _matches_weak,
    _tags,
    etag_of,
)
from grelmicro.http._gate import DECLARATION_KEY
from grelmicro.http._idempotency import (
    StoredResponse,
    _authenticated_scope,
    _declarations_of,
    _declared_pattern,
    _gate_path_matches,
    _templates,
)
from grelmicro.http._routes import RouteDeclaration, route_name
from grelmicro.types import BackendScope

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

_SHARED = "__grelmicro_response_cache_shared__"
"""Attribute the declared dependency carries, saying the response is shared."""

_UNMARKED: Any = object()
"""Marks a route that declared nothing, which a `None` TTL cannot."""

_AUTH_MISSING: Any = object()
"""Marks an authentication field absent before child dispatch."""

_AUTH_UNREADABLE: Any = object()
"""Marks an authentication attribute that could not be inspected safely."""

_SAFE_METHODS = frozenset({"GET", "HEAD"})
"""Methods a response cache answers. Everything else passes through."""

_HTTP_200_OK = 200
"""The one status a response cache stores."""

_HTTP_304_NOT_MODIFIED = 304
"""Status answering a read whose entity tag the client already holds."""

_DEFAULT_TTL = timedelta(seconds=60)
"""How long a response is kept when neither the route nor the component says."""

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


@dataclass(frozen=True)
class _AuthenticationSnapshot:
    """Authentication objects and identity state visible before dispatch."""

    user: Any
    auth: Any
    user_state: tuple[Any, ...]
    auth_state: tuple[Any, ...]


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


def _durations_per_path(value: Any) -> Any:  # noqa: ANN401
    """Read each lifetime a mapping of patterns gives, named by position.

    A tuple of patterns passes through. A refusal names the pattern by
    its position, never by the pattern itself.

    Raises:
        ValueError: If a pattern names a lifetime that is not a valid
            duration.
    """
    if not isinstance(value, abc.Mapping):
        return value
    return {
        pattern: read_duration(ttl, f"pattern {position} in include")
        for position, (pattern, ttl) in enumerate(value.items(), start=1)
    }


def declare_cached(
    ttl: Annotated[
        int | timedelta | None,
        Doc("How long the route's response is served from the cache."),
    ],
    *,
    shared: Annotated[
        bool,
        Doc("Every caller the route admits is served the same response."),
    ] = False,
) -> Callable[[], Awaitable[None]]:
    """Return the callable a route declares to have its response cached.

    `grelmicro.integrations.fastapi.CachedResponse` wraps it in a
    `Depends`, which is how a route says so, and the FastAPI integration
    declares it as the route's `cache`, and `shared` as the route's
    `shared`. It computes nothing: what it carries is the TTL, whether it
    is shared, and where it is declared.

    Raises:
        ValueError: If `ttl` is not whole seconds or a `timedelta`, is not
            greater than zero, or is over 100 years.
    """
    duration = check_duration(ttl, "ttl") if ttl is not None else None

    async def cached_response() -> None:
        """Declare that this route's response is cached.

        Async so the framework resolves it on the event loop. A sync
        dependency goes through a worker thread, and this one is a
        declaration with nothing in it to run there.
        """

    setattr(cached_response, _MARKER, duration)
    setattr(cached_response, _SHARED, shared)
    return cached_response


def declared_cache(call: object) -> bool | timedelta:
    """Return what a dependency declares of its route's response cache.

    `False` for one that is not `CachedResponse()`, `True` for one keeping
    the response for the component's TTL, and its own TTL otherwise.
    """
    ttl = getattr(call, _MARKER, _UNMARKED)
    if ttl is _UNMARKED:
        return False
    return True if ttl is None else ttl


def declares_shared(call: object) -> bool:
    """Return whether a dependency is a `CachedResponse(shared=True)`."""
    return bool(getattr(call, _SHARED, False))


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


class _Policies:
    """The paths a middleware caches, and for how long.

    Built from two sources that answer the same question. `include` names
    URLs and is written where the component is registered. The routes come
    from what the app's integration declares, read at install, again when
    the app starts, and again on a request once a route was added.

    At a route whose gate admitted the request, the declaration the gate
    carries answers for that route instead, and nothing is matched against
    the routes.
    """

    __slots__ = (
        "_declared",
        "_exclude",
        "_include",
        "_refused",
        "_refused_paths",
        "_refused_templates",
        "_root",
        "_source",
        "_topology",
    )

    def __init__(
        self,
        include: Annotated[
            Mapping[str, timedelta] | Sequence[str],
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

        A bare string, and a lifetime that is not a duration, are refused
        before this: by the config on the component's door, and by the
        middleware on the hand-wired one.
        """
        # A sequence names the paths and leaves the lifetime to the
        # component, which is how every other middleware reads. `None`
        # stands for "whatever the component says", the same as a route
        # that declared no lifetime of its own.
        items: list[tuple[str, timedelta | None]] = (
            [(pattern, None) for pattern in include]
            if not isinstance(include, abc.Mapping)
            else list(include.items())
        )
        # Most specific first: an exact path beats a prefix, and a longer
        # prefix beats the shorter one it sits under, so a rule written
        # for one route is not answered by the one written for its router.
        self._include: tuple[tuple[str, timedelta | None], ...] = tuple(
            sorted(
                items,
                key=lambda item: (item[0].endswith("*"), -len(item[0])),
            )
        )
        self._exclude = exclude
        self._declared: tuple[
            tuple[str, Pattern[str], timedelta | None], ...
        ] = ()
        self._refused: tuple[tuple[str, Pattern[str]], ...] = ()
        self._refused_paths: frozenset[str] = frozenset()
        self._refused_templates: tuple[str, ...] = ()
        self._source: Any = None
        self._root = False
        self._topology: _RouteTopologyState | None = None

    def _is_refused(self, path: str) -> bool:
        """Return whether the app refuses to have this path cached.

        A read whose route runs checks of its own, or sits under checks
        between this middleware and the route. A hit is answered before
        they run, so caching it answers over them.
        """
        # A path holding a control character is refused whatever it names:
        # Starlette's `$` matches before a final newline, so it can reach a
        # literal route the set below holds without that newline.
        return (
            path in self._refused_paths
            or holds_control_character(path)
            or any(
                _gate_path_matches(template, regex, path)
                for template, regex in self._refused
            )
        )

    def _refuses_template(self, path: str) -> bool:
        """Return whether a refusal covers a declared route template."""
        return path in self._refused_templates or any(
            _gate_path_matches(template, regex, path)
            for template, regex in self._refused
        )

    def read(
        self,
        app: Annotated[Any, Doc("The application to read the rules off.")],  # noqa: ANN401
        *,
        include_root_middleware: bool = False,
    ) -> None:
        """Read what every route of the app declares, as its integration lists it.

        Without `include_root_middleware`, the routes are read from the
        app's router, so middleware the app runs around it, which runs
        before this one, does not count as checks.

        Raises:
            TypeError: If a route declaring a cache answers a method other
                than `GET`, or runs checks of its own, or a path `include`
                names answers no `GET`.
        """
        self._source = app
        self._root = include_root_middleware
        self._read()

    def refresh(
        self,
        *apps: Any,  # noqa: ANN401
        source: Any = None,  # noqa: ANN401, ARG002
    ) -> None:
        """Read the routes again once a route was added, moved or replaced.

        An app passed when none was read yet is read.
        """
        if self._source is None:
            found = next((app for app in apps if app is not None), None)
            if found is not None:
                self.read(found)
            return
        topology = self._topology
        if topology is None or topology.changed():
            self._read()

    def reread(self) -> None:
        """Read the app again, for the routes added since install."""
        if self._source is not None:
            self._read()

    def renewed(
        self,
        include: Mapping[str, timedelta] | Sequence[str],
        exclude: tuple[str, ...],
    ) -> _Policies:
        """Return the rules `include` and `exclude` give, read off the same app.

        Raises:
            TypeError: If a path `include` names answers no `GET`.
        """
        policies = _Policies(include, exclude)
        if self._source is not None:
            policies.read(self._source, include_root_middleware=self._root)
        return policies

    def _read(self) -> None:
        """Read and publish what the source declares, from one snapshot of its routes.

        Raises:
            TypeError: If a route declaring a cache answers a method other
                than `GET`, or runs checks of its own, or a path `include`
                names answers no `GET`.
        """
        source = self._source
        listed = source if self._root else _router_of(source)
        declarations = _declarations_of((listed,))
        declared: list[tuple[str, Pattern[str], timedelta | None]] = []
        refused: list[tuple[str, Pattern[str]]] = []
        for declaration in declarations:
            _refuse_uncacheable(declaration)
            methods = declaration.methods
            if methods is not None and methods.isdisjoint(_SAFE_METHODS):
                continue
            checked = declaration.own_checks or declaration.checked_above
            for template in _templates(declaration):
                regex = _declared_pattern(template)
                if checked:
                    refused.append((template, regex))
                elif declaration.cache is not False:
                    cache = declaration.cache
                    declared.append(
                        (template, regex, None if cache is True else cache)
                    )
        _refuse_named_write(
            tuple(pattern for pattern, _ in self._include), declarations
        )
        self._declared = tuple(declared)
        # A route declared with no parameter answers one path, so it is
        # a set lookup. Only a template standing for many needs its
        # regex asked, and an app has few of those beside its literals.
        self._refused_paths = frozenset(
            template for template, _ in refused if "{" not in template
        )
        self._refused_templates = tuple(template for template, _ in refused)
        self._refused = tuple(
            (template, regex) for template, regex in refused if "{" in template
        )
        self._topology = _RouteTopologyState(source)

    def pattern_ttl(
        self,
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[timedelta, Doc("The component's own TTL.")],
    ) -> timedelta | None:
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
        yes, so a request that no pattern names costs no scan of it.
        """
        ttl = self._named(path, default)
        if ttl is None or self._is_refused(path):
            return None
        return ttl

    def _named(self, path: str, default: timedelta) -> timedelta | None:
        """Return how long the most specific pattern naming `path` keeps it."""
        for pattern, ttl in self._include:
            if matches(path, (pattern,)):
                return default if ttl is None else ttl
        return None

    def ttl_for(
        self,
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[timedelta, Doc("The component's own TTL.")],
    ) -> timedelta | None:
        """Return how long this path is cached before routing, or `None` when it is not.

        A route that declared one says more than a pattern naming it, so
        `include=` fills in for the routes that declared none.

        The refusal is asked only once something would otherwise be
        kept, so a request nothing names costs no scan of it.
        """
        for template, regex, marked in self._declared:
            if _gate_path_matches(template, regex, path):
                return (
                    None
                    if self._is_refused(path)
                    else (default if marked is None else marked)
                )
        return self.pattern_ttl(path, default)

    def ttl_at(
        self,
        declaration: Annotated[
            RouteDeclaration,
            Doc("What the route the gate admitted the request to declares."),
        ],
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[timedelta, Doc("The component's own TTL.")],
    ) -> timedelta | None:
        """Return how long the route keeps this response, or `None` for never.

        Its own `cache` decides. A route running checks of its own is
        never cached by a pattern, and any other route is cached for as
        long as the pattern naming the path says.
        """
        cache = declaration.cache
        if cache is not False:
            return default if cache is True else cache
        if declaration.own_checks:
            return None
        return self._named(path, default)


def _router_of(app: Any) -> Any:  # noqa: ANN401
    """Return the router an app routes with, or the app when it has none."""
    router = getattr(app, "router", None)
    return app if router is None else router


def _refuse_uncacheable(declaration: RouteDeclaration) -> None:
    """Refuse a route declaring a cache it cannot honor.

    Raises:
        TypeError: If the route answers a method other than `GET` or
            `HEAD`, or runs checks of its own before the handler.
    """
    if declaration.cache is False:
        return
    route = route_name(declaration)
    methods = declaration.methods
    if methods is None or not methods <= _SAFE_METHODS:
        answered = (
            "every method" if methods is None else ", ".join(sorted(methods))
        )
        msg = (
            f"{route} declares a cached response and answers {answered}. "
            "A response cache answers a read, and a method that changes "
            "something must reach the handler every time. Declare it on "
            "the GET route instead."
        )
        raise TypeError(msg)
    if declaration.own_checks:
        msg = (
            f"{route} declares a cached response and runs checks of its own "
            "before the handler, such as a dependency or a security scheme. "
            "A hit is answered before they run, so one caller's response "
            "would be handed to whoever asks next. Cache a route that "
            "answers everybody the same."
        )
        raise TypeError(msg)


def _refuse_named_write(
    named: tuple[str, ...],
    declarations: list[RouteDeclaration],
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
        methods: set[str] = set()
        for declaration in declarations:
            template = declaration.path
            if declaration.methods is None or not names_route(
                pattern, template, _declared_pattern(template)
            ):
                continue
            methods |= declaration.methods
        if not methods or "GET" in methods:
            continue
        listed = ", ".join(sorted(methods))
        msg = (
            f"include= names {pattern!r}, which answers {listed} and no "
            f"GET. Only a read is cached, so this pattern would cache "
            f"nothing. Name a path that answers a GET, or leave it out."
        )
        raise TypeError(msg)


def declared_ttl(
    route: Annotated[Any, Doc("The route to read the declaration off.")],  # noqa: ANN401
    contexts: Annotated[
        tuple[Any, ...],
        Doc("What was declared above it, outermost first."),
    ],
    declared: Annotated[str, Doc("The full path the route sits under.")],  # noqa: ARG001
) -> tuple[bool, timedelta | None]:
    """Return whether this route declares a TTL, and the TTL it names.

    The nearest declaration decides: the route's own beats the router it
    sits in, and an inner router beats the one that includes it. `None`
    seconds means the declaration named none, so the component's own TTL
    applies.

    Read off the route rather than matched against its path, because a
    path is compiled before it matches anything: a route declared as
    `/products/{pid:int}` is a pattern, not a URL, and matching one
    against the other answers `no` for every typed converter.

    A route answering anything but `GET` declares none, as the cache
    leaves it to its handler. A read running checks of its own is left
    out by what the app's routes declare, which the report asks first.
    """
    ttl = _declared_ttl(route, _declaring_above(contexts))
    for context in reversed(contexts):
        if ttl is not _UNMARKED:
            break
        ttl = _inherited_ttl(context)
    methods = {
        method.upper() for method in (getattr(route, "methods", None) or ())
    } - {"HEAD", "OPTIONS"}
    if ttl is _UNMARKED or methods != {"GET"}:
        return False, None
    return True, ttl


def _declared_ttl(route: Any, above: set[int]) -> Any:  # noqa: ANN401
    """Return the TTL this route declared, or `_UNMARKED` for one that did not.

    Read off the resolved dependency tree, under the framework's own
    spelling of it. A framework that resolves none declares nothing here,
    and names its paths in `include=` instead.

    What a router declared through its own constructor is resolved into
    every route it holds, so `above` says which of them the route did not
    write itself.
    """
    for call in declared_dependencies(route):
        ttl = getattr(call, _MARKER, _UNMARKED)
        if ttl is not _UNMARKED and id(call) not in above:
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
    mapping gives each its own lifetime.
    """

    ttl: Annotated[
        Duration,
        Doc(
            "How long a response is kept when its route names none, in "
            "whole seconds or as a `timedelta`. A float is refused. From "
            'text it reads whole seconds (`"60"`) or an ISO 8601 duration '
            '(`"PT0.5S"`).'
        ),
    ] = _DEFAULT_TTL
    include: Annotated[
        PathPatterns | Mapping[str, Duration],
        BeforeValidator(_durations_per_path),
        Doc(
            "The paths cached. A tuple keeps each for `ttl`, and a "
            'mapping such as `{"/products/*": 60}` gives each its own '
            "lifetime, read like `ttl`. The most specific pattern decides."
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

    At a route whose gate admitted the request, the route's declaration
    decides. A protected route declaring `cache` shares its response
    among every caller it admits, a credential included, keyed by the
    protection it declares. A request carrying a credential to an
    anonymous route is answered by its handler. A route running checks of
    its own is never cached by a pattern. A `Cookie` still keeps a request
    out.

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
            int | timedelta,
            Doc(
                "How long a response is kept when its route names none, "
                "in whole seconds or as a `timedelta`."
            ),
        ] = _DEFAULT_TTL,
        include: Annotated[
            Mapping[str, int | timedelta] | tuple[str, ...] | None,
            Doc(
                "The paths cached. A tuple keeps each for `ttl`, and a "
                "mapping gives each its own lifetime. Exact match unless "
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
        lock: Annotated[
            BackendScope | None,
            Doc(
                "How far concurrent misses on one response share a single "
                'call of the handler. `"process"` (default) folds them in '
                'the process. `"host"` or `"cluster"` also fold them '
                "through the lock backend of the app's `Coordination`, the "
                "same as `@cached(lock=...)`. `None` turns folding off. Not "
                "live: it is read when the middleware is built."
            ),
        ] = "process",
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
                else _Policies(config.include, config.exclude)
            )
            owned_policies.read(app, include_root_middleware=True)
            self._live = Live(_state_of(config, owned_policies))
        self._warned: set[str] = set()
        self._reported: dict[str, float] = {}
        self._unstorable: OrderedDict[str, None] = OrderedDict()
        scope = check_fold(lock)
        self._fold = None if scope is None else Fold("CachedResponses", scope)

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
        authentication = _authentication_snapshot(scope)
        declaration: RouteDeclaration | None = scope.get(DECLARATION_KEY)
        if declaration is None:
            if _carries_credentials(scope) or _asks_for_part(scope):
                await self.app(scope, receive, send)
                return
            state.policies.refresh(scope.get("app"), source=self.app)
            ttl = state.policies.ttl_for(path, config.ttl)
        else:
            shared = declaration.shared and declaration.cache is not False
            if (
                _carries_cookie(scope)
                if shared
                else _carries_credentials(scope)
            ) or _asks_for_part(scope):
                await self.app(scope, receive, send)
                return
            ttl = state.policies.ttl_at(declaration, path, config.ttl)
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
            scope,
            receive,
            send,
            state=state,
            key=built
            if declaration is None
            else _protected(declaration, built),
            ttl=ttl,
            path=path,
            authentication=authentication,
        )

    async def _answer(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        state: _State,
        key: str,
        ttl: timedelta,
        path: str,
        authentication: _AuthenticationSnapshot,
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
            attempt.entry = self._entry_of(
                capture,
                state=state,
                scope=scope,
                path=path,
                authentication=authentication,
            )
            attempt.returned = True
            if attempt.entry is None:
                raise _NotStored
            await self._write(
                storage_key,
                attempt.entry,
                ttl=_kept_for(ttl, attempt.entry["kept"]),
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
                    fold=self._fold,
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
        self, storage_key: str, entry: _Entry, *, ttl: timedelta
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
            _request_scheme(scope),
            _request_authority(scope),
            _request_root_path(scope),
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
        self,
        capture: _ResponseCapture,
        *,
        state: _State,
        scope: Scope,
        path: str,
        authentication: _AuthenticationSnapshot,
    ) -> _Entry | None:
        """Return the entry this response is stored as, or `None` to skip it.

        An `Age` the response carries is not stored, so a hit answers the
        one it counts itself.
        """
        start = capture.start
        if start is None or capture.released or not capture.complete:
            return None
        if _authentication_changed(authentication, scope):
            # An opaque wrapped application may run authentication below this
            # middleware, where route inspection cannot discover it. Added,
            # replaced, or mutated authentication state proves that child
            # dispatch crossed such a boundary. Even an anonymous result
            # cannot be stored: a later authenticated request would otherwise
            # read it before that inner authentication runs.
            return None
        if start["status"] != _HTTP_200_OK:
            return None
        headers = [
            (name, value)
            for name, value in start["headers"]
            if name.lower() != b"age"
        ]
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


def _kept_for(ttl: timedelta, kept: float) -> timedelta:
    """Return how long a response is stored: its TTL, or less if it says so.

    `kept` is the freshness the response names, in seconds, or `inf` when
    it names none. A freshness shorter than the TTL is rounded up to the
    second, and never past the TTL.
    """
    if kept >= ttl.total_seconds():
        return ttl
    return min(ttl, timedelta(seconds=math.ceil(kept)))


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


def _carries_cookie(scope: Scope) -> bool:
    """Return whether the request carries a cookie."""
    return any(name.lower() == b"cookie" for name, _ in scope["headers"])


def _protected(declaration: RouteDeclaration, key: str) -> str:
    """Return `key` under the protection the route declares.

    A response stored while the route was anonymous, or required other
    scopes, is never read once it requires a caller or these scopes.
    """
    if declaration.anonymous:
        return f"anonymous\x00{key}"
    return f"authenticated {' '.join(sorted(declaration.scopes))}\x00{key}"


def _authentication_attribute(value: Any, name: str) -> Any:  # noqa: ANN401
    """Read one conventional authentication attribute without trusting it."""
    try:
        return getattr(value, name, _AUTH_MISSING)
    except Exception:  # noqa: BLE001
        return _AUTH_UNREADABLE


def _authentication_state(value: Any, *, user: bool) -> tuple[Any, ...]:  # noqa: ANN401
    """Snapshot conventional user or credential state without object equality."""
    if value is _AUTH_MISSING:
        return ()
    if user:
        return (
            type(value),
            _authentication_attribute(value, "is_authenticated"),
            _authentication_attribute(value, "identity"),
            _authentication_attribute(value, "display_name"),
            _authentication_attribute(value, "username"),
        )
    scopes = _authentication_attribute(value, "scopes")
    if scopes is not _AUTH_MISSING and scopes is not _AUTH_UNREADABLE:
        try:
            scopes = tuple(scopes)
        except TypeError, ValueError:
            scopes = _AUTH_UNREADABLE
    return type(value), scopes


def _authentication_snapshot(scope: Scope) -> _AuthenticationSnapshot:
    """Remember authentication presence, object identity, and public state."""
    user = scope.get("user", _AUTH_MISSING)
    auth = scope.get("auth", _AUTH_MISSING)
    return _AuthenticationSnapshot(
        user=user,
        auth=auth,
        user_state=_authentication_state(user, user=True),
        auth_state=_authentication_state(auth, user=False),
    )


def _authentication_changed(
    before: _AuthenticationSnapshot,
    scope: Scope,
) -> bool:
    """Return whether child dispatch added, replaced, or mutated auth state."""
    user = scope.get("user", _AUTH_MISSING)
    auth = scope.get("auth", _AUTH_MISSING)
    return (
        user is not before.user
        or auth is not before.auth
        or _authentication_state(user, user=True) != before.user_state
        or _authentication_state(auth, user=False) != before.auth_state
    )


def _query_of(scope: Scope, selected: tuple[str, ...] | None) -> str:
    """Return an injective query view with repeated-value order preserved."""
    raw = scope.get("query_string", b"").decode("latin-1")
    pairs = parse_qsl(raw, keep_blank_values=True)
    if selected is None:
        grouped: dict[str, list[str]] = {}
        for name, value in pairs:
            grouped.setdefault(name, []).append(value)
        material: list[tuple[str, list[str] | None]] = [
            (name, grouped[name]) for name in sorted(grouped)
        ]
    else:
        grouped = {}
        for name, value in pairs:
            grouped.setdefault(name, []).append(value)
        material = [
            (name, grouped.get(name))
            for name in sorted({unquote_plus(name) for name in selected})
        ]
    return json.dumps(material, ensure_ascii=True, separators=(",", ":"))


def _header_of(scope: Scope, name: str) -> str:
    """Return an injective view of all occurrences of one request header."""
    wanted = name.encode("latin-1")
    values = [
        value.decode("latin-1")
        for key, value in scope["headers"]
        if key.lower() == wanted
    ]
    return json.dumps(values or None, separators=(",", ":"))


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
    framework that declares no cache on its routes.

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
            int | timedelta | None,
            Doc(
                "How long a response is kept when its route names none, in "
                "whole seconds or as a `timedelta`. A float is refused. "
                "`CachedResponse(ttl=...)` overrides it per route."
            ),
        ] = None,
        include: Annotated[
            Mapping[str, int | timedelta] | tuple[str, ...] | None,
            Doc(
                "The paths cached, for a route that declares none. A "
                "tuple keeps each for `ttl`, and a mapping gives each its "
                'own lifetime, as `{"/products/*": 60}`. Exact match '
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
        lock: Annotated[
            BackendScope | None,
            Doc(
                "How far concurrent misses on one response share a single "
                'call of the handler. `"process"` (default) folds them in '
                'the process. `"host"` or `"cluster"` also fold them '
                "through the lock backend of the app's `Coordination`, the "
                "same as `@cached(lock=...)`. `None` turns folding off. Not "
                "live: it is read when the middleware is built."
            ),
        ] = "process",
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
                not whole seconds or a `timedelta`, is not greater than
                zero, or is over 100 years.
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
            lock=lock,
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
        lock: Annotated[
            BackendScope | None,
            Doc("How far concurrent misses on one response fold."),
        ] = "process",
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
            lock=lock,
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
        lock: BackendScope | None,
    ) -> None:
        """Hold the configuration, the store, and the middleware's cell.

        Raises:
            SettingsValidationError: If `lock` is not a backend scope or
                `None`.
        """
        self._lock = check_fold(lock)
        self._name = name
        self._key = key
        self._skip = skip
        self._cache: TTLCache[Any] = (
            cache
            if cache is not None
            else TTLCache(
                ttl=config.ttl,
                name=name,
                serializer=JsonSerializer(),
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

        The new patterns are read against what the app's routes declare
        first, the same reading `micro.install(app)` does. A path written
        out that answers no read is refused, and a read running checks of
        its own is left uncached. Reload has to preserve both decisions: a
        mounted file must not be able to start caching what the static
        path would not.
        """
        policies = self._policies.renewed(
            new_config.include, new_config.exclude
        )
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
            "lock": self._lock,
            "live": self._live,
        }

    def read_routes(
        self,
        app: Annotated[Any, Doc("The application to read the rules off.")],  # noqa: ANN401
    ) -> None:
        """Read what every route of the app declares, as its integration lists it.

        Called by the integration after the middleware is added. The app
        is read again when it starts, so a route added between the two
        counts as well.

        Raises:
            TypeError: If a route declaring a cache answers a method other
                than `GET`, or runs checks of its own, or a path `include`
                names answers no `GET`.
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
