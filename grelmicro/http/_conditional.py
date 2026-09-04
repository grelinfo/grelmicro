"""Conditional requests.

`ConditionalRequests` registers the middleware that binds each request's
preconditions and puts an `ETag` on the response, and `check_precondition`
is what a handler calls to refuse a write whose `If-Match` no longer
matches.

Read more in the [Conditional Requests](../http/conditional.md) docs.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    Self,
    cast,
)
from uuid import UUID

from typing_extensions import Doc

from grelmicro._guards import is_instance, type_name
from grelmicro._paths import as_patterns, route_path, selects
from grelmicro.errors import OutOfContextError
from grelmicro.http._component import ErrorResponses, send_error
from grelmicro.http._kinds import (
    BODYLESS_STATUSES,
    PRECONDITION_REQUIRED,
    Kind,
    Occurrence,
)
from grelmicro.http.errors import (
    PreconditionError,
    PreconditionFailedError,
    PreconditionRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping, Sequence
    from types import TracebackType

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "ConditionalRequests",
    "ConditionalRequestsMiddleware",
    "check_freshness",
    "check_precondition",
    "etag_of",
]


_ETAG_CHARS = re.compile(r"^[\x21\x23-\x7e]*$")
"""What an entity tag may hold, per RFC 9110: no quote, no control byte."""

_WEAK_PREFIX = "W/"
"""Marks an entity tag that compares equal only under weak comparison."""

_SEPARATOR = ","
"""What separates two entity tags in one header, so no tag may hold it."""

_ANY = "*"
"""The wildcard tag, which matches whatever the resource currently is."""

_SAFE_METHODS = frozenset({"GET", "HEAD"})
"""Methods a `304` may answer, because they change nothing."""

_HTTP_304_NOT_MODIFIED = 304
"""Status answering a read whose entity tag the client already holds."""

_MIN_SUCCESS = 200
"""Lowest status an entity tag may describe."""

_MAX_SUCCESS = 299
"""Highest status an entity tag may describe."""

_KEPT_ON_304 = frozenset(
    {
        b"cache-control",
        b"content-location",
        b"date",
        b"etag",
        b"expires",
        b"vary",
    }
)
"""Headers a `304` carries, per RFC 9110. The rest describe content it has none of."""

_request: ContextVar[_Conditional] = ContextVar("grelmicro_conditional")
"""The current request's conditional state, bound by the middleware."""

_UNSET: Final = object()
"""Marks the version argument as not given, which `None` cannot.

`None` is the resource that does not exist, and that is a real answer the
guard acts on, so the absent argument needs a marker of its own.
"""


def etag_of(
    value: Annotated[
        object,
        Doc(
            """
            What identifies this version of the resource.

            A version token (`int`, `str`, `UUID`, `datetime`) becomes the
            entity tag itself, and a representation (`bytes`, a mapping, a
            sequence, or a pydantic model) is hashed into one.
            """
        ),
    ],
    *,
    weak: Annotated[
        bool,
        Doc(
            "Mark the tag weak. A weak tag answers `304` on a read and "
            "never matches an `If-Match`, which takes strong comparison."
        ),
    ] = False,
) -> str:
    """Build an entity tag for a resource.

    `check_freshness` and `check_precondition` build one for you, so a
    handler reaches for this only when it needs the tag itself: to set one
    on a response the middleware will not tag, to hand one to
    `check_precondition(etag=...)`, to store one beside a cached object, or
    to compare tags in a client of another service.

    Two ways in, because a service has one of two things at hand:

    ```python
    etag_of(cart.version)  # a version column: "7"
    etag_of(cart)  # a pydantic model: "b1946ac9..."
    ```

    A version token is used as it stands, since it already identifies the
    version and hashing it would only make it longer. A representation is
    serialized and hashed with SHA-256.

    | Value | Tag |
    |---|---|
    | `int`, `str`, `UUID` | the value, quoted |
    | `datetime` | its ISO 8601 form, quoted |
    | `bytes` | SHA-256 of the bytes |
    | a pydantic model, dict, list | SHA-256 of the canonical JSON |

    `weak=True` marks the tag weak, which says equivalent rather than byte
    for byte. A weak tag still answers `304` on a read and never satisfies
    an `If-Match`, which takes strong comparison.

    Prefer a version token. A hash of the representation changes whenever
    the serialization does, so adding a field to the model changes every
    entity tag your service has ever issued, and every client holding one
    gets `412` until it fetches again.

    The serialization is canonical: sorted keys, no spaces, and never the
    JSON library that happens to be installed, so every replica produces
    the same tag for the same value. A pydantic model goes through
    `model_dump(mode="json")` first.

    Read more in the [Conditional Requests](../http/conditional.md) docs.

    Raises:
        TypeError: If the value is a `bool`, or is neither a version token
            nor something that serializes to JSON.
        ValueError: If a version token holds a quote or a control
            character, which an entity tag cannot carry. An entity tag that
            is already one goes to `check_precondition(etag=...)` instead.
    """
    tag = f'"{_token(value)}"'
    return f"{_WEAK_PREFIX}{tag}" if weak else tag


def _token(value: object) -> str:
    """Return the opaque part of the entity tag for `value`.

    Every shape test goes through the guards: `isinstance` reads
    `__class__`, which a lazy proxy raises from, and a version handed to
    this is caller data.
    """
    if is_instance(value, (bytes, bytearray)):
        return hashlib.sha256(cast("bytes", value)).hexdigest()
    if is_instance(value, bool):
        # Before `int`, which it subclasses. A boolean version is a
        # mistake worth naming rather than turning into "True".
        msg = "etag_of() takes a version or a representation, not a bool."
        raise TypeError(msg)
    if is_instance(value, (str, int, UUID)):
        return _checked(str(value))
    if is_instance(value, datetime):
        return _checked(cast("datetime", value).isoformat())
    return hashlib.sha256(_canonical(value)).hexdigest()


def _checked(token: str) -> str:
    """Return `token` when an entity tag can carry it.

    Raises:
        ValueError: If it holds a quote or a control character.
    """
    if not token or not _ETAG_CHARS.match(token) or _SEPARATOR in token:
        msg = (
            f"etag_of() cannot build an entity tag from {token!r}: it must "
            f"be a non-empty value without quotes, commas, or control "
            f"characters. "
            f"Pass the representation to have it hashed, or, if this is "
            f"already an entity tag, check_precondition(etag=...)."
        )
        raise ValueError(msg)
    return token


def _canonical(value: object) -> bytes:
    """Serialize a representation the same way on every replica.

    `sort_keys` so a dict built in another order hashes the same, compact
    separators so whitespace never counts, and the standard library rather
    than whichever JSON library is installed, because `orjson` and `json`
    disagree on spacing and a mixed fleet would issue two entity tags for
    one resource.

    Raises:
        TypeError: If the value does not serialize to JSON.
    """
    payload = value
    dump = cast("Callable[..., Any] | None", _attribute(value, "model_dump"))
    if callable(dump):
        # A pydantic model renders its own UUIDs, datetimes and decimals,
        # which is what makes the result plain JSON data.
        payload = dump(mode="json")
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=_unsupported,
        ).encode()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # Serializing runs caller code: `__class__` on the way in, and
        # `keys`, `__iter__` or a `model_dump` property on the way
        # through. Whatever it raises, the answer is the same: this is not
        # something an entity tag can be built from.
        msg = (
            f"etag_of() takes a version token, bytes, a pydantic model, or "
            f"JSON data, got {type_name(value)}."
        )
        raise TypeError(msg) from exc


def _attribute(value: object, name: str) -> object | None:
    """Read one attribute of a caller's value, and never raise doing it.

    A property runs caller code, and a lazily-bound object raises from one
    the moment it is read. Answering None sends the value to the same
    place a missing attribute does.
    """
    try:
        return getattr(value, name, None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return None


def _unsupported(value: object) -> str:
    """Refuse a value JSON cannot carry, rather than guessing at it."""
    msg = f"not JSON data: {type_name(value)}"
    raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class _Preconditions:
    """What the request asked to be true before it runs.

    `None` means the header was absent, which is different from a header
    carrying no usable tag.
    """

    if_match: tuple[str, ...] | None = None
    if_none_match: tuple[str, ...] | None = None

    def check(self, current: str | None, *, require: bool) -> None:
        """Evaluate the preconditions against the resource as it is now.

        The order is RFC 9110's: `If-Match` first, then `If-None-Match`,
        then the service's own requirement that the request be
        conditional.

        Raises:
            PreconditionFailedError: If a precondition does not hold.
            PreconditionRequiredError: If `require` and neither header
                was sent.
        """
        if self.if_match is not None:
            if not _matches_strong(self.if_match, current):
                raise PreconditionFailedError
            return
        if self.if_none_match is not None:
            if _matches_weak(self.if_none_match, current):
                raise PreconditionFailedError
            return
        if require:
            raise PreconditionRequiredError


@dataclass(slots=True)
class _Conditional:
    """What this request asked, and what the handler answered with.

    `etag` is empty until a handler records one with `check_freshness`. The
    middleware puts it on the response and compares against it, rather than
    hashing the body it would otherwise have to buffer.
    """

    preconditions: _Preconditions
    etag: str | None = None


def _matches_strong(tags: tuple[str, ...], current: str | None) -> bool:
    """Return whether one tag matches under strong comparison.

    `If-Match` takes strong comparison, so a weak tag never matches, and
    the wildcard matches any resource that exists.
    """
    if current is None:
        # Nothing to match: the resource is not there, so no tag of it can
        # be the one the client holds.
        return False
    if _ANY in tags:
        return True
    if current.startswith(_WEAK_PREFIX):
        return False
    return any(
        tag == current and not tag.startswith(_WEAK_PREFIX) for tag in tags
    )


def _matches_weak(tags: tuple[str, ...], current: str | None) -> bool:
    """Return whether one tag matches under weak comparison.

    `If-None-Match` takes weak comparison, so `W/"x"` and `"x"` are the
    same tag, and the wildcard matches any resource that exists.
    """
    if current is None:
        return False
    if _ANY in tags:
        return True
    opaque = _opaque(current)
    return any(_opaque(tag) == opaque for tag in tags)


def _opaque(tag: str) -> str:
    """Return the tag without its weakness marker."""
    return tag.removeprefix(_WEAK_PREFIX)


def check_precondition(
    version: Annotated[
        object,
        Doc(
            """
            What identifies the version the resource carries now.

            Whatever `etag_of` takes: a version token (`int`, `str`,
            `UUID`, `datetime`) or a representation (`bytes`, JSON data, a
            pydantic model). `None` says the resource does not exist,
            which is what makes `If-None-Match: *` a create.
            """
        ),
    ] = _UNSET,
    *,
    etag: Annotated[
        str | None,
        Doc(
            "An entity tag that is already one, quotes included. For a tag "
            "read from a store or an upstream service rather than built "
            "here. Pass this or the version, never both."
        ),
    ] = None,
    require: Annotated[
        bool,
        Doc(
            "Answer `428` when the request carries no precondition at all. "
            "Default `True`: a handler that checks is a handler whose "
            "write must be conditional."
        ),
    ] = True,
) -> None:
    """Refuse a write whose precondition no longer holds.

    Load the resource, hand over what identifies its version, and write
    only if this returns:

    ```python
    @app.put("/carts/{cart_id}")
    async def replace(cart_id: str, body: CartIn) -> Cart:
        cart = await repo.load(cart_id)
        check_precondition(cart.version)
        return await repo.save(cart.apply(body))
    ```

    The entity tag is built for you, the same way `etag_of` builds one, so
    a version column is all a handler has to hand over. Pass the whole
    resource where there is no version column, and `etag=` where the tag
    is already a tag.

    This is a check, not a lock. Between it and the write, another request
    can land, so the write itself has to be conditional too. Read
    [Conditional Requests](../http/conditional.md) for the three ways to
    do that, and which one to reach for.

    The request's headers come from `ConditionalRequests()`, so nothing is
    threaded through the handler signature and the same line works on
    every framework.

    Raises:
        PreconditionFailedError: If the client's entity tag is not the
            one the resource carries. Answers `412`.
        PreconditionRequiredError: If `require` and the request carried
            no precondition. Answers `428`.
        OutOfContextError: If `ConditionalRequests()` is not registered,
            so no request headers were ever read.
        TypeError: If neither the version nor `etag` is given, or both
            are.
    """
    current = _current(version, etag)
    _bound("check_precondition").preconditions.check(current, require=require)


def _check_sent_precondition(
    if_match: str | None,
    if_none_match: str | None,
    version: object = _UNSET,
    *,
    etag: str | None = None,
    require: bool = True,
) -> None:
    """Evaluate preconditions read somewhere other than the request scope.

    What the FastAPI dependency calls: it declares the two headers itself,
    so it answers from those rather than from the middleware's binding, and
    a route that injects it works whether or not the component is
    registered.

    Raises:
        PreconditionFailedError: If the client's entity tag is not the one
            the resource carries.
        PreconditionRequiredError: If `require` and neither header arrived.
    """
    _Preconditions(
        if_match=_split(if_match),
        if_none_match=_split(if_none_match),
    ).check(_current(version, etag), require=require)


def _split(value: str | None) -> tuple[str, ...] | None:
    """Return the entity tags of one header value, or None when absent."""
    if value is None:
        return None
    tags = tuple(tag.strip() for tag in value.split(_SEPARATOR) if tag.strip())
    return tags or None


def _current(version: object, etag: str | None) -> str | None:
    """Return the entity tag the resource carries, from either door.

    Raises:
        TypeError: If neither door was used, or both were.
    """
    if etag is not None:
        if version is not _UNSET:
            msg = (
                "Pass the version or etag=, not both. Pass the version and "
                "the entity tag is built for you."
            )
            raise TypeError(msg)
        return etag
    if version is _UNSET:
        msg = (
            "Pass what identifies the resource's version, such as "
            "check_precondition(cart.version). Pass None when the resource "
            "does not exist yet."
        )
        raise TypeError(msg)
    return None if version is None else etag_of(version)


def check_freshness(
    version: Annotated[
        object,
        Doc(
            """
            What identifies the version the resource carries now.

            Whatever `etag_of` takes: a version token (`int`, `str`,
            `UUID`, `datetime`) or a representation (`bytes`, JSON data, a
            pydantic model).
            """
        ),
    ] = _UNSET,
    *,
    etag: Annotated[
        str | None,
        Doc(
            "An entity tag that is already one, quotes included. Pass this "
            "or the version, never both."
        ),
    ] = None,
) -> bool:
    """Answer this read with an entity tag, and say whether it changed.

    A write can only be conditional if the read handed out a tag first, so
    this is the other half of `check_precondition`:

    ```python
    @app.get("/carts/{cart_id}")
    async def read(cart_id: str) -> Cart:
        version = await repo.version(cart_id)  # one cheap column
        check_freshness(version)
        return await repo.load(cart_id)
    ```

    The response carries the tag, with no `Response` object in the handler
    signature and no header string to spell. A client that sends it back
    in `If-None-Match` is answered `304 Not Modified` with no body.

    Returns `True` when the client already holds this version, so a
    handler that can skip work does:

    ```python
    if check_freshness(version):
        raise HTTPException(status_code=304)
    ```

    Ignoring the return value is correct too: the answer is the same `304`,
    it just costs the work of building a body nobody reads.

    Recording a tag also spares the middleware hashing the response body,
    which it does only for a handler that recorded none.

    Raises:
        OutOfContextError: If `ConditionalRequests()` is not registered,
            so no request headers were ever read.
        TypeError: If neither the version nor `etag` is given, or both
            are.
    """
    current = _current(version, etag)
    conditional = _bound("check_freshness")
    conditional.etag = current
    tags = conditional.preconditions.if_none_match
    return tags is not None and _matches_weak(tags, current)


def _bound(caller: str) -> _Conditional:
    """Return the current request's conditional state.

    Raises:
        OutOfContextError: If the middleware never bound a request.
    """
    try:
        return _request.get()
    except LookupError:
        raise OutOfContextError(_OUT_OF_CONTEXT_HINT.format(caller)) from None


_OUT_OF_CONTEXT_HINT = (
    "{}() read no request. Register ConditionalRequests() in uses=[...] "
    "and call micro.install(app), so the middleware binds each request's "
    "If-Match and If-None-Match headers."
)


class ConditionalRequestsMiddleware:
    """Bind each request's preconditions and put an `ETag` on the response.

    Three things, none of which a handler should have to write:

    - Every `If-Match` and `If-None-Match` header is parsed and bound for
      the request, so `check_precondition(...)` reads them with no request
      object in the handler signature.
    - A `2xx` response that carries a complete body and no `ETag` of its
      own gets one, hashed from the body it just produced.
    - A `GET` or `HEAD` whose `If-None-Match` matches that tag is answered
      `304 Not Modified` with no body.
    - A method named in `require_precondition` that carries neither
      precondition header is answered `428`, before it reaches a handler.

    ```python
    from grelmicro import Grelmicro
    from grelmicro.http import ConditionalRequestsMiddleware

    app.add_middleware(ConditionalRequestsMiddleware)
    ```

    Register `ConditionalRequests()` instead to have `micro.install(app)`
    add it for you.

    The `304` saves the response, not the work: the handler has already
    run by the time the body is there to hash. A handler that knows its
    resource's version cheaply should compare it itself and skip the load.

    A response over `max_body_size`, or one streamed in chunks past it, is
    forwarded as it comes and gets no entity tag, so a large download is
    never held in memory.

    The middleware is pure ASGI and works with any ASGI framework
    (Starlette, Litestar, ...). It acts on `http` scopes and passes every
    other scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[
            ASGIApp,
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        etag_responses: Annotated[
            bool,
            Doc(
                "Add an `ETag` to a `2xx` response that carries a complete "
                "body and none of its own."
            ),
        ] = True,
        require_precondition: Annotated[
            Sequence[str],
            Doc(
                """
                Methods answered `428` when they carry no precondition.

                `("PUT", "PATCH", "DELETE")` refuses every unconditional
                write in the app, before it reaches a handler. `POST` is
                left out of that set on purpose: a create has nothing to
                match against yet.

                Either header counts, so `If-None-Match: *` still creates
                under enforcement. Empty by default, which leaves the
                decision to `check_precondition()` per route.
                """
            ),
        ] = (),
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
        max_body_size: Annotated[
            int,
            Doc(
                "Largest response body held in memory to hash, in bytes. "
                "A larger one is forwarded untouched."
            ),
        ] = 1024 * 1024,
    ) -> None:
        """Initialize the middleware with its entity tag policy."""
        self.app = app
        self._etag_responses = etag_responses
        self._require_precondition = frozenset(
            method.upper() for method in require_precondition
        )
        self._include = tuple(include)
        self._exclude = tuple(exclude)
        self._max_body_size = max_body_size

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Bind the preconditions, then shape the response around them."""
        if scope["type"] != "http" or not selects(
            route_path(scope), include=self._include, exclude=self._exclude
        ):
            await self.app(scope, receive, send)
            return

        headers = scope["headers"]
        preconditions = _Preconditions(
            if_match=_tags(headers, b"if-match"),
            if_none_match=_tags(headers, b"if-none-match"),
        )
        if scope["method"] in self._require_precondition and (
            preconditions.if_match is None
            and preconditions.if_none_match is None
        ):
            await _refuse(send, scope, PRECONDITION_REQUIRED)
            return

        conditional = _Conditional(preconditions)
        token = _request.set(conditional)
        try:
            shaper = _ResponseShaper(
                send,
                conditional=conditional,
                max_body_size=self._max_body_size,
                readable=scope["method"] in _SAFE_METHODS,
                hash_body=self._etag_responses,
            )
            await self.app(scope, receive, shaper)
            await shaper.flush()
        finally:
            _request.reset(token)


class _ResponseShaper:
    """Shape one response: tag it, or answer `304` in place of it.

    A status cannot change once it is on the wire, so the decision has to
    be made while the response is still here. It is held only as long as
    that can pay off, which is while the body arrives in one piece and
    stays under `max_body_size`. A streamed response goes out as it comes.

    A handler that recorded a version needs none of that: its entity tag
    is known before the first byte, so even a stream carries it, and even
    a stream is swapped for a `304` when the client already holds it.
    """

    def __init__(
        self,
        send: Send,
        *,
        conditional: _Conditional,
        max_body_size: int,
        readable: bool,
        hash_body: bool,
    ) -> None:
        """Initialize the shaper around the downstream `send`."""
        self._send = send
        self._conditional = conditional
        self._max_body_size = max_body_size
        self._readable = readable
        self._hash_body = hash_body
        self._start: Message | None = None
        self._chunks: list[bytes] = []
        self._size = 0
        self._released = False
        self._answered = False

    async def __call__(self, message: Message) -> None:
        """Shape the response, or forward what is no longer shapeable."""
        if self._answered:
            # A `304` went out in place of this response, so what the app
            # is still producing has nowhere to go.
            return
        if self._released:
            await self._send(message)
            return
        if message["type"] == "http.response.start":
            self._start = message
            if message.get("trailers"):
                # Trailers follow the body, so a response that declares
                # them cannot be held back and reordered.
                await self._release()
            return
        if message["type"] != "http.response.body":
            # A message this does not shape. Whatever is held goes first,
            # so nothing reaches the client out of order.
            await self._release()
            await self._forward(message)
            return
        if message.get("more_body", False):
            # A body arriving in pieces is one the app is streaming.
            # Holding it to hash it would turn an event stream into one
            # message at the end.
            await self._release()
            await self._forward(message)
            return
        chunk = message.get("body", b"")
        self._size += len(chunk)
        if self._size > self._max_body_size:
            await self._release()
            await self._forward(message)
            return
        self._chunks.append(chunk)
        # Decided here rather than after the app returns: a framework runs
        # background tasks inside that call, and a response held until they
        # finish is a response the client waits for.
        await self._decide()

    async def flush(self) -> None:
        """Send whatever is still held once the app has returned.

        Only an app that stopped without finishing its body reaches this,
        since a complete response is decided the moment it completes.
        """
        if self._released or self._answered or self._start is None:
            return
        await self._release()

    async def _decide(self) -> None:
        """Answer with the entity tag, a `304`, or what the handler sent."""
        start = self._start
        if start is None:  # pragma: no cover
            return
        body = b"".join(self._chunks)
        etag = self._tag(body)
        if etag is not None and self._not_modified(etag):
            await self._send_not_modified(etag)
            self._released = True
            self._answered = True
            return
        if etag is not None and _own_etag(start) is None:
            start["headers"] = [
                *start["headers"],
                (b"etag", etag.encode("latin-1")),
            ]
        await self._release(complete=True)

    def _tag(self, body: bytes) -> str | None:
        """Return the entity tag this response is identified by, if any.

        The handler's own wins, then the version it recorded, then a hash
        of the body. A `304` the handler answered itself is tagged too,
        which is what RFC 9110 asks of one.
        """
        start = self._start
        if start is None:  # pragma: no cover
            return None
        own = _own_etag(start)
        recorded = self._conditional.etag
        if start["status"] == _HTTP_304_NOT_MODIFIED:
            return own or recorded
        if not _carries_a_representation(start["status"]):
            return None
        if own is not None:
            return own
        if recorded is not None:
            return recorded
        return etag_of(body) if self._hash_body else None

    def _not_modified(self, etag: str) -> bool:
        """Return whether the client already holds this representation."""
        tags = self._conditional.preconditions.if_none_match
        status = self._start["status"] if self._start else 0
        return (
            self._readable
            and _carries_a_representation(status)
            and tags is not None
            and _matches_weak(tags, etag)
        )

    async def _send_not_modified(self, etag: str) -> None:
        """Answer `304` with the headers a bodyless response may carry."""
        start = self._start
        if start is None:  # pragma: no cover
            return
        headers = [
            (name, value)
            for name, value in start["headers"]
            if name.lower() in _KEPT_ON_304 and name.lower() != b"etag"
        ]
        headers.append((b"etag", etag.encode("latin-1")))
        await self._send(
            {
                "type": "http.response.start",
                "status": _HTTP_304_NOT_MODIFIED,
                "headers": headers,
            }
        )
        await self._send({"type": "http.response.body", "body": b""})

    async def _forward(self, message: Message) -> None:
        """Send a message unless a `304` already answered this request."""
        if self._answered:
            return
        await self._send(message)

    async def _release(self, *, complete: bool = False) -> None:
        """Send what is held and stop shaping.

        `complete` says the held chunks are the whole body, which is the
        one case where this closes the response rather than leaving it
        open for what the app is still sending.

        A recorded version tags the response and can still answer `304`
        here, because neither needs the body.
        """
        if self._released:  # pragma: no cover
            return
        start = self._start
        recorded = self._conditional.etag
        if start is not None and recorded is not None:
            if self._not_modified(recorded):
                await self._send_not_modified(recorded)
                self._released = True
                self._answered = True
                return
            if (
                _carries_a_representation(start["status"])
                and _own_etag(start) is None
            ):
                start["headers"] = [
                    *start["headers"],
                    (b"etag", recorded.encode("latin-1")),
                ]
        self._released = True
        if start is not None:  # pragma: no branch
            await self._send(start)
        if self._chunks or complete:
            await self._send(
                {
                    "type": "http.response.body",
                    "body": b"".join(self._chunks),
                    "more_body": not complete,
                }
            )


def _own_etag(start: Message) -> str | None:
    """Return the entity tag the handler set itself, if it set one."""
    for name, value in start["headers"]:
        if name.lower() == b"etag":
            return value.decode("latin-1")
    return None


def _carries_a_representation(status: int) -> bool:
    """Return whether a response with this status has one to tag.

    A failure describes nothing to tag, and a `204` carries no
    representation, so hashing its empty body would give every empty
    resource in the app the same entity tag.
    """
    return (
        _MIN_SUCCESS <= status <= _MAX_SUCCESS
        and status not in BODYLESS_STATUSES
    )


def _tags(
    headers: Sequence[tuple[bytes, bytes]], name: bytes
) -> tuple[str, ...] | None:
    """Return the entity tags of one header, or None when it is absent.

    A header present but empty reads as absent: it asks for nothing, and
    treating it as a list of no tags would refuse every request.
    """
    values = [
        raw_value.decode("latin-1").strip()
        for raw_name, raw_value in headers
        if raw_name.lower() == name
    ]
    if not values:
        return None
    tags = tuple(
        tag.strip()
        for value in values
        for tag in value.split(",")
        if tag.strip()
    )
    return tags or None


async def _refuse(send: Send, scope: Scope, kind: Kind) -> None:
    """Answer a request the middleware refuses itself, before the app runs.

    A middleware sits outside the routing layer, so no exception handler
    sees what it decides. It renders through whichever `ErrorResponses`
    the app registered, so what the schema publishes for this response is
    what the wire returns.
    """
    app = scope.get("app")
    registered = getattr(
        getattr(app, "state", None), "grelmicro_error_responses", None
    )
    errors = registered if registered is not None else ErrorResponses()
    rendered = errors._render_occurrence(  # noqa: SLF001
        Occurrence(kind), instance=scope["path"]
    )
    await send_error(send, rendered)


class ConditionalRequests:
    """Answer conditional requests, wired by `micro.install(app)`.

    Register it and `install` adds `ConditionalRequestsMiddleware`, so
    every request's preconditions are bound for `check_precondition(...)`
    and every response carries an `ETag`:

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.http import ConditionalRequests, ErrorResponses

    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()
    micro.install(app)
    ```

    Every option of `ConditionalRequestsMiddleware` is taken here and
    forwarded, so a registered component and a hand-added middleware
    answer the same.

    A framework that serves no HTTP, such as FastStream, ignores it.

    Read more in the [Conditional Requests](../http/conditional.md) docs.
    """

    kind: ClassVar[str] = "conditional_requests"

    def __init__(
        self,
        *,
        etag_responses: Annotated[
            bool,
            Doc(
                "Add an `ETag` to a `2xx` response that carries a complete "
                "body and none of its own."
            ),
        ] = True,
        require_precondition: Annotated[
            Sequence[str],
            Doc(
                "Methods answered `428` when they carry no precondition. "
                '`("PUT", "PATCH", "DELETE")` refuses every unconditional '
                "write in the app. Empty by default, which leaves the "
                "decision to `check_precondition()` per route."
            ),
        ] = (),
        include: Annotated[
            tuple[str, ...],
            Doc(
                "Paths this middleware acts on. Empty means every path. "
                "Name the prefix of a router to select it, as "
                '`"/payments/*"`.'
            ),
        ] = (),
        exclude: Annotated[
            tuple[str, ...],
            Doc(
                "Paths this middleware leaves alone, whatever `include` "
                "says. Same matching."
            ),
        ] = (),
        max_body_size: Annotated[
            int,
            Doc("Largest response body held in memory to hash, in bytes."),
        ] = 1024 * 1024,
        openapi: Annotated[
            bool,
            Doc(
                "Describe `If-Match`, `If-None-Match` and the responses "
                "they lead to in the OpenAPI schema, so a client built "
                "from it sends the headers and Swagger offers the fields. "
                "Only FastAPI builds one, and every other framework "
                "ignores this."
            ),
        ] = True,
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
    ) -> None:
        """Answer conditional requests through the registered middleware."""
        self._name = name
        self._openapi = openapi
        self._options: dict[str, Any] = {
            "etag_responses": etag_responses,
            "require_precondition": require_precondition,
            "include": as_patterns(include, name="include"),
            "exclude": as_patterns(exclude, name="exclude"),
            "max_body_size": max_body_size,
        }

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with."""
        return ConditionalRequestsMiddleware, dict(self._options)

    def document_openapi(
        self,
        app: Annotated[Any, Doc("The FastAPI application to describe.")],  # noqa: ANN401
    ) -> None:
        """Describe the conditional headers in the app's OpenAPI schema.

        Called by the FastAPI integration after the middleware is added. A
        framework that builds no schema never calls it.
        """
        if not self._openapi:
            return
        from grelmicro.integrations.fastapi import (  # noqa: PLC0415
            document_conditional_requests,
        )

        document_conditional_requests(app)

    def handled_exceptions(self) -> tuple[type[Exception], ...]:
        """Return what this component answers rather than letting through.

        Registering it is the opt-in: a service that asked for conditional
        requests gets `412` and `428` on the wire, not a `500`, whether or
        not an `ErrorResponses` is registered. That component chooses the
        format, this one decides these two are answered at all.
        """
        return (PreconditionError,)

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
