"""Response cache for HTTP reads."""

from __future__ import annotations

import hashlib
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

from grelmicro._paths import as_patterns, matches, route_path
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

_UNCACHEABLE_DIRECTIVES = frozenset({"no-store", "no-cache", "private"})
"""`Cache-Control` directives that refuse the store outright."""

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


def declare_cached(
    ttl: Annotated[
        float | None, Doc("Seconds the route's response is served from.")
    ],
) -> Callable[[], None]:
    """Return the callable a route declares to have its response cached.

    `grelmicro.integrations.fastapi.CachedResponse` wraps it in a
    `Depends`, which is how a route says so, and `micro.install(app)`
    reads it back off the dependency tree. It computes nothing: what it
    carries is the TTL, and where it is declared.
    """

    def cached_response() -> None:
        """Declare that this route's response is cached."""

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
        """Hold the path rules, with no routes read yet."""
        self._paths = tuple(paths.items())
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
        self._routes = tuple(_marked_routes(app))

    def reread(self) -> None:
        """Read the app again, for the routes added since install."""
        if self._app is not None:
            self.read(self._app)

    def ttl_for(
        self,
        path: Annotated[str, Doc("The path the request is asking for.")],
        default: Annotated[float, Doc("The component's own TTL.")],
    ) -> float | None:
        """Return how long this path is cached, or `None` when it is not."""
        for pattern, ttl in self._paths:
            if matches(path, (pattern,)):
                return ttl
        for regex, marked in self._routes:
            if regex.fullmatch(path):
                return default if marked is None else marked
        return None


def _marked_routes(app: Any) -> list[tuple[Pattern[str], float | None]]:  # noqa: ANN401
    """Return the compiled path of every route that declared a TTL.

    Walks what the app declares, mounts included, and compiles each full
    path with the framework's own compiler, so a path parameter matches
    exactly what the router matches it with.

    Raises:
        TypeError: If a marked route answers a method other than `GET`.
    """
    from starlette.routing import compile_path  # noqa: PLC0415

    found: list[tuple[Pattern[str], float | None]] = []
    for prefix, route in _walk(app, ""):
        ttl = _declared_ttl(route)
        if ttl is _UNMARKED:
            continue
        methods = {
            method.upper() for method in (getattr(route, "methods", None) or ())
        } - {"HEAD", "OPTIONS"}
        if methods != {"GET"}:
            listed = ", ".join(sorted(methods)) or "no method"
            msg = (
                f"CachedResponse() is declared on "
                f"{prefix}{route.path_format!r}, "
                f"which answers {listed}. A response cache answers a read, "
                "and a method that changes something must reach the "
                "handler every time. Mark the GET route instead."
            )
            raise TypeError(msg)
        regex, _, _ = compile_path(f"{prefix}{route.path_format}")
        found.append((regex, cast("float | None", ttl)))
    return found


def _declared_ttl(route: Any) -> Any:  # noqa: ANN401
    """Return the TTL this route declared, or `_UNMARKED` for one that did not.

    Read off the resolved dependency tree, under the framework's own
    spelling of it. A framework that resolves none declares nothing here,
    and names its paths in `paths=` instead.
    """
    declared = getattr(route, "dependant", None)  # codespell:ignore
    for dependency in getattr(declared, "dependencies", ()):
        ttl = getattr(dependency.call, _MARKER, _UNMARKED)
        if ttl is not _UNMARKED:
            return ttl
    return _UNMARKED


def _walk(app: Any, prefix: str) -> list[tuple[str, Any]]:  # noqa: ANN401
    """Return every route the app declares, with the prefix it sits under."""
    found: list[tuple[str, Any]] = []
    for route in getattr(app, "routes", ()):
        inner = getattr(route, "routes", None)
        if inner:
            found.extend(_walk(route, f"{prefix}{getattr(route, 'path', '')}"))
            continue
        if getattr(route, "path_format", None) is not None:
            found.append((prefix, route))
    return found


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
    it for you, and to mark routes with `@cache_response(ttl=...)`.

    A miss runs the handler once. Every other request for the same key
    waits for that one and is answered from what it stored, in process and
    across replicas, so a cold key never fans one computation out to every
    caller at once.

    A response is stored only when it is safe to hand to somebody else:
    status `200`, no `Set-Cookie`, no `Content-Encoding`, no
    `Cache-Control` refusing it, and a `Vary` naming nothing outside
    `vary_by_headers`. A request carrying `Authorization` or `Cookie`
    never reads the cache and never fills it.

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
        self._vary_by_headers = tuple(name.lower() for name in vary_by_headers)
        self._vary_by_query = vary_by_query
        self._key = key
        self._skip = skip
        self._max_body_size = max_body_size
        self._tag = tag
        self._warned: set[str] = set()

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
        if _carries_credentials(scope) or _refuses_the_cache(scope):
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
        fresh = _asks_for_a_fresh_answer(scope)
        if not fresh:
            entry = cast("_Entry | None", await self._cache.get(storage_key))
            if entry is not None:
                await _serve(entry, scope, send)
                return
        capture = _ResponseCapture(send, max_body_size=self._max_body_size)
        stores = scope["method"] == "GET"

        async def run() -> _Entry:
            await self.app(scope, receive, capture)
            await capture.flush()
            stored = self._entry_of(capture, path=path) if stores else None
            if stored is None:
                raise _NotStored
            return stored

        try:
            if fresh:
                entry = await run()
                await self._cache.set(
                    storage_key, entry, ttl, tags=(self._tag,)
                )
            else:
                entry = cast(
                    "_Entry",
                    await self._cache.get_or_set(
                        storage_key, run, ttl=ttl, tags=(self._tag,)
                    ),
                )
        except _NotStored:
            await capture.release(complete=capture.complete)
            return
        await _serve(entry, scope, send)

    def _built(self, scope: Scope, path: str) -> str:
        """Return the key this request reads, from its path and its vary rules."""
        parts = [path, _query_of(scope, self._vary_by_query)]
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
        named = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in headers
        }
        if not self._storable(named, path=path):
            return None
        body = capture.body
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
        return _Entry(
            status=start["status"],
            headers=[
                [name.decode("latin-1"), value.decode("latin-1")]
                for name, value in headers
            ],
            body=body.decode("latin-1"),
            stored_at=clock_time(),
        )

    def _storable(self, headers: dict[str, str], *, path: str) -> bool:
        """Return whether this response may be handed to another caller."""
        if "set-cookie" in headers or "content-encoding" in headers:
            return False
        directives = {
            directive.strip().lower()
            for directive in headers.get("cache-control", "").split(",")
        }
        if directives & _UNCACHEABLE_DIRECTIVES:
            return False
        vary = headers.get("vary")
        if vary is None:
            return True
        named = [
            name.strip().lower() for name in vary.split(",") if name.strip()
        ]
        if "*" in named:
            self._warn(path, vary)
            return False
        undeclared = sorted(set(named) - set(self._vary_by_headers))
        if undeclared:
            self._warn(path, vary)
            return False
        return True

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
        self.answered = False
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
            await self.release()
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
        self.answered = True
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


def _carries_credentials(scope: Scope) -> bool:
    """Return whether the request is one caller's, so no cache may answer it."""
    return any(
        name.lower() in _PRIVATE_REQUEST_HEADERS for name, _ in scope["headers"]
    )


def _refuses_the_cache(scope: Scope) -> bool:
    """Return whether the client wants no store involved at all."""
    return "no-store" in _request_directives(scope)


def _asks_for_a_fresh_answer(scope: Scope) -> bool:
    """Return whether the client wants the handler run again.

    `no-cache` asks for a fresh answer, not for the store to be skipped,
    so what the handler says is kept for the callers after it.
    """
    return "no-cache" in _request_directives(scope)


def _request_directives(scope: Scope) -> set[str]:
    """Return the `Cache-Control` directives the request carries."""
    found: set[str] = set()
    for name, value in scope["headers"]:
        if name.lower() != b"cache-control":
            continue
        found.update(
            directive.strip().lower()
            for directive in value.decode("latin-1").split(",")
        )
    return found


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
        """Answer repeated reads through the registered middleware."""
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
            "vary_by_headers": tuple(vary_by_headers),
            "vary_by_query": vary_by_query,
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
