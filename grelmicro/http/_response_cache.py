"""Response cache for HTTP reads."""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
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

from typing_extensions import Doc

from grelmicro._paths import as_patterns, matches, route_path, walk_routes
from grelmicro.cache._stampede import compute_with_stampede
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.http._conditional import (
    _KEPT_ON_304,
    _matches_weak,
    _tags,
    etag_of,
)
from grelmicro.http._idempotency import StoredResponse

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        Mapping,
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


class _Policies:
    """The paths a middleware caches, and for how long.

    Built from two sources that answer the same question. `paths` names
    URLs and is written where the component is registered. The routes come
    from `@cache_response`, and are read off the app at install and again
    when the app starts, so a route added after `install` counts too.
    """

    __slots__ = ("_app", "_paths", "_routes")

    def __init__(
        self,
        paths: Annotated[
            Mapping[str, float], Doc("Path patterns and their TTL.")
        ],
    ) -> None:
        """Hold the path rules, with no routes read yet.

        Raises:
            ValueError: If a pattern names a lifetime a response cannot
                be kept for.
        """
        if isinstance(paths, str):
            msg = (
                f"paths={paths!r} is a string, and a mapping of path "
                "pattern to seconds is expected. Write it as one: "
                f"paths={{{paths!r}: 60}}."
            )
            raise TypeError(msg)
        for pattern, ttl in paths.items():
            check_ttl(ttl, f"paths[{pattern!r}]")
        # Most specific first: an exact path beats a prefix, and a longer
        # prefix beats the shorter one it sits under, so a rule written
        # for one route is not answered by the one written for its router.
        self._paths = tuple(
            sorted(
                paths.items(),
                key=lambda item: (item[0].endswith("*"), -len(item[0])),
            )
        )
        self._routes: tuple[tuple[Pattern[str], float | None], ...] = ()
        self._app: Any = None

    def read(
        self,
        app: Annotated[Any, Doc("The application to read the rules off.")],  # noqa: ANN401
    ) -> None:
        """Read every route the app declares that asked to be cached.

        Raises:
            TypeError: If a marked route answers a method other than `GET`.
        """
        self._app = app
        self._routes = tuple(
            _marked_routes(app, tuple(pattern for pattern, _ in self._paths))
        )

    def reread(self) -> None:
        """Read the app again, for the routes added since install."""
        if self._app is not None:
            self.read(self._app)

    def ttl_for(
        self,
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[float, Doc("The component's own TTL.")],
    ) -> float | None:
        """Return how long this path is cached, or `None` when it is not.

        A route that declared one says more than a pattern naming it, so
        `paths=` fills in for the routes that declared none.
        """
        for regex, marked in self._routes:
            if regex.fullmatch(path):
                return default if marked is None else marked
        for pattern, ttl in self._paths:
            if matches(path, (pattern,)):
                return ttl
        return None


def _marked_routes(
    app: Any,  # noqa: ANN401
    named: tuple[str, ...] = (),
) -> list[tuple[Pattern[str], float | None]]:
    """Return the compiled path of every route that declared a TTL.

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
    for prefix, route, contexts in walk_routes(app):
        above = _declaring_above(contexts)
        ttl = _declared_ttl(route, above)
        on_the_route = ttl is not _UNMARKED
        for context in reversed(contexts):
            if ttl is not _UNMARKED:
                break
            ttl = _inherited_ttl(context)
        declared = f"{prefix}{route.path}"
        if ttl is _UNMARKED:
            if matches(declared, named):
                _refuse_named_gate(route, contexts, declared)
            continue
        refusal = _unreadable(route, contexts, declared)
        if refusal is not None:
            if on_the_route:
                raise TypeError(refusal)
            # A router declares it for what it holds, and holds more than
            # reads. What cannot be answered from a cache is left to its
            # handler rather than refused.
            continue
        regex, _, _ = compile_path(declared)
        found.append((regex, cast("float | None", ttl)))
    return found


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
        f"paths= names {declared!r}, which is gated by {named}. A hit is "
        "answered before the app is routed, so the gate would not run, "
        "and one caller's response would be handed to whoever asks next. "
        "Name a path that answers everybody the same."
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
    and names its paths in `paths=` instead.

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
        paths={"/products/*": 60},
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
    never reads the cache and never fills it.

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
                "`micro.install(app)`. `paths` alone needs none."
            ),
        ] = None,
        ttl: Annotated[
            float,
            Doc("Seconds a response is kept when its route names none."),
        ] = _DEFAULT_TTL,
        paths: Annotated[
            Mapping[str, float] | None,
            Doc(
                "Path patterns and the seconds each is cached for. Exact "
                'match unless the pattern ends with `*`, as `"/products/*"`.'
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
    ) -> None:
        """Initialize the middleware with the paths it answers for."""
        self.app = app
        self._cache = cache
        self._policies = (
            policies if policies is not None else _Policies(paths or {})
        )
        self._ttl = ttl
        self._exclude = as_patterns(exclude, name="exclude")
        self._vary_by_headers = tuple(
            name.lower()
            for name in as_patterns(vary_by_headers, name="vary_by_headers")
        )
        self._vary_by_query = (
            None
            if vary_by_query is None
            else as_patterns(vary_by_query, name="vary_by_query")
        )
        self._key = key
        self._skip = skip
        self._max_body_size = max_body_size
        self._tag = tag
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
        path = route_path(scope)
        if matches(path, self._exclude):
            await self.app(scope, receive, send)
            return
        ttl = self._policies.ttl_for(path, self._ttl)
        if ttl is None:
            await self.app(scope, receive, send)
            return
        if _carries_credentials(scope) or _asks_for_part(scope):
            await self.app(scope, receive, send)
            return
        built = (
            self._key(scope)
            if self._key is not None
            else self._built(scope, path)
        )
        if built is None:
            await self.app(scope, receive, send)
            return
        await self._answer(scope, receive, send, key=built, ttl=ttl, path=path)

    async def _answer(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
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
        capture = _ResponseCapture(send, max_body_size=self._max_body_size)
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
            attempt.entry = self._entry_of(capture, path=path)
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

    def _built(self, scope: Scope, path: str) -> str:
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
            _query_of(scope, self._vary_by_query),
        ]
        parts.extend(_header_of(scope, name) for name in self._vary_by_headers)
        return "\x00".join(parts)

    def _storage_key(self, key: str) -> str:
        """Return the cache key one request's key is stored under."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self._tag}:{digest}"

    def _entry_of(
        self, capture: _ResponseCapture, *, path: str
    ) -> _Entry | None:
        """Return the entry this response is stored as, or `None` to skip it."""
        start = capture.start
        if start is None or capture.released or not capture.complete:
            return None
        if start["status"] != _HTTP_200_OK:
            return None
        headers = list(start["headers"])
        kept = self._storable(headers, path=path)
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
        headers = self._declaring_vary(headers)
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
        self, headers: list[tuple[bytes, bytes]]
    ) -> list[tuple[bytes, bytes]]:
        """Return the headers with what the key reads named in `Vary`.

        The middleware keys on these, so the response does vary on them,
        and everything downstream has to be told: a CDN, a proxy, or the
        browser's own cache would otherwise hand one caller's copy to the
        next one who sent a different value.
        """
        if not self._vary_by_headers:
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
            + list(self._vary_by_headers)
        )
        kept.append((b"vary", ", ".join(named).encode("latin-1")))
        return kept

    def _storable(
        self, headers: Sequence[tuple[bytes, bytes]], *, path: str
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
            if "*" in varies or set(varies) - set(self._vary_by_headers):
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
    return any(
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


class CachedResponses:
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

    The bare form caches nothing until a route declares it. `paths=` names
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
            float,
            Doc(
                "Seconds a response is kept when its route names none. "
                "`CachedResponse(ttl=...)` overrides it per route."
            ),
        ] = _DEFAULT_TTL,
        paths: Annotated[
            Mapping[str, float] | None,
            Doc(
                "Path patterns and the seconds each is cached for, for a "
                "route that carries no mark. Exact match unless the "
                'pattern ends with `*`, as `"/products/*"`.'
            ),
        ] = None,
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths never cached, whatever a mark or `paths` says. "
                "Same matching."
            ),
        ] = (),
        vary_by_headers: Annotated[
            tuple[str, ...],
            Doc(
                "Request headers whose value is part of the key. A "
                "response whose `Vary` names a header outside this set is "
                "not stored, because one value would answer another."
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
            Doc("Largest response body stored, in bytes."),
        ] = _DEFAULT_MAX_BODY_SIZE,
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
                "rules on one app never read each other's responses."
            ),
        ] = "http",
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
    ) -> None:
        """Answer repeated reads through the registered middleware.

        Raises:
            ValueError: If `ttl`, or one a pattern names, is not a
                positive number of seconds.
        """
        check_ttl(ttl, "ttl")
        self._name = name
        self._cache: TTLCache[Any] = (
            cache
            if cache is not None
            else TTLCache(ttl=ttl, serializer=JsonSerializer())
        )
        self._policies = _Policies(paths or {})
        self._tag = f"grelmicro:{namespace}:{name}"
        self._options: dict[str, Any] = {
            "cache": self._cache,
            "policies": self._policies,
            "ttl": ttl,
            "exclude": as_patterns(exclude, name="exclude"),
            "vary_by_headers": as_patterns(
                vary_by_headers, name="vary_by_headers"
            ),
            "vary_by_query": (
                None
                if vary_by_query is None
                else as_patterns(vary_by_query, name="vary_by_query")
            ),
            "key": key,
            "skip": skip,
            "max_body_size": max_body_size,
            "tag": self._tag,
        }

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
        """Return the middleware class and the arguments to build it with."""
        return CachedResponsesMiddleware, dict(self._options)

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
