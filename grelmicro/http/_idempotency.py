"""Idempotent requests.

`IdempotencyMiddleware` replays a stored response when a request repeats its
idempotency key, and `IdempotentRequests` registers it through `uses=[...]`
so `micro.install(app)` adds it.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
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

from grelmicro._guards import is_instance, type_name
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
from grelmicro.http._paths import selects
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


_MIN_CONTENT_STATUS = 200
"""Lowest status that may carry content."""


_KEY_SEPARATOR = "\x1f"
"""Separator joining the parts of a stored key."""


def _field_name(value: str, argument: str, example: str) -> str:
    """Return `value`, or raise when it is not an HTTP field name."""
    if not is_instance(value, str):
        msg = f"{argument} must be a string, got {type_name(value)}."
        raise SettingsValidationError(msg)
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

    Four kinds of response are not stored, and each one lets a retry
    re-run the handler: one carrying `Set-Cookie`, one carrying
    `Content-Encoding`, one declaring trailers, and one whose body is
    over `max_body_size`. All four are logged. Pass `skip` to add a rule
    of your own.

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
                the client key, so two routes never replay each other.
                **Set this in any multi-tenant app**, folding in the
                caller identity. Without it a client that learns another
                client's key replays their response.
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
    ) -> None:
        """Initialize the middleware with the idempotency store and policy."""
        self.app = app
        self._idempotency = idempotency
        self._header = _key_name(key_header).lower().encode("ascii")
        self._header_name = key_header
        self._replay_header = (
            _replay_name(replay_header).lower().encode("ascii")
        )
        self._replay_header_name = replay_header
        self._replay_collision_logged = False
        self._methods = frozenset(method.upper() for method in methods)
        self._key_maker = key_maker
        self._skip = skip
        self._require_key = require_key
        self._fingerprint_body = fingerprint_body
        self._max_body_size = max_body_size
        self._wait_timeout = wait_timeout
        self._include = tuple(include)
        self._exclude = tuple(exclude)
        self._reused = (
            IDEMPOTENCY_KEY_REUSED
            if reused_status == IDEMPOTENCY_KEY_REUSED.status
            else replace(IDEMPOTENCY_KEY_REUSED, status=reused_status)
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Replay, execute, or pass the request through."""
        if (
            scope["type"] != "http"
            or scope["method"] not in self._methods
            or not selects(
                scope["path"], include=self._include, exclude=self._exclude
            )
        ):
            await self.app(scope, receive, send)
            return

        key = _header_value(scope["headers"], self._header)
        if not key:
            if self._require_key:
                await _refuse(
                    send,
                    scope,
                    IDEMPOTENCY_KEY_INVALID,
                    f"The {self._header_name} header is required on this "
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
                f"The {self._header_name} header is longer than "
                f"{_MAX_KEY_LENGTH} characters.",
            )
            return

        if not _KEY_CHARS.match(key):
            await _refuse(
                send,
                scope,
                IDEMPOTENCY_KEY_INVALID,
                f"The {self._header_name} header holds a character it "
                f"cannot carry. Use printable ASCII, such as a UUID.",
            )
            return

        fingerprint = None
        if self._fingerprint_body:
            body, too_large, receive = await _buffer_request(
                receive, self._max_body_size
            )
            if too_large:
                await _refuse(send, scope, REQUEST_BODY_TOO_LARGE)
                return
            if body is not None:
                fingerprint = hashlib.sha256(body).hexdigest()

        await self._execute(
            scope, receive, send, self._storage_key(scope, key), fingerprint
        )

    async def _execute(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        storage_key: str,
        fingerprint: str | None,
    ) -> None:
        """Run the request under the idempotency block, or replay it."""
        block = self._idempotency(
            storage_key,
            fingerprint=fingerprint,
            wait_timeout=self._wait_timeout,
        )
        try:
            operation = await block.__aenter__()
        except IdempotencyConflictError:
            await _refuse(
                send,
                scope,
                self._reused,
                f"The {self._header_name} header was already used with a "
                f"different request payload. Use a fresh key, or resend the "
                f"original payload.",
            )
            return
        except IdempotencyWaitTimeoutError:
            await _refuse(
                send,
                scope,
                IDEMPOTENCY_IN_FLIGHT,
                f"A request with this {self._header_name} is still running. "
                f"Retry after the delay in the Retry-After header to read "
                f"its response.",
                retry_after=_IN_FLIGHT_RETRY_AFTER,
            )
            return
        except OutOfContextError as exc:
            raise OutOfContextError(_OUT_OF_CONTEXT_HINT) from exc

        try:
            if operation.replayed:
                replaced = await _send_stored(
                    send,
                    operation.result(),
                    head=scope["method"] == "HEAD",
                    replay_header=self._replay_header,
                )
                if replaced and not self._replay_collision_logged:
                    self._replay_collision_logged = True
                    _logger.warning(
                        "Replay marker replaced the %s header the stored "
                        "response carried. Give replay_header a name of "
                        "its own where that value matters.",
                        self._replay_header_name,
                    )
            else:
                capture = _ResponseCapture(
                    send, self._max_body_size, self._skip
                )
                await self.app(scope, receive, capture)
                if capture.stored is not None:
                    operation.store(capture.stored)
        except BaseException as exc:
            await block.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await block.__aexit__(None, None, None)

    def _storage_key(self, scope: Scope, key: str) -> str:
        """Build the stored key, scoped by route unless `key_maker` says otherwise."""
        if self._key_maker is not None:
            return _checked_key(self._key_maker(scope, key), key)
        parts = [scope["method"], scope["path"]]
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
    ) -> None:
        """Initialize the capture around the downstream `send`."""
        self._send = send
        self._max_body_size = max_body_size
        self._skip = skip
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
        for name, _value in message["headers"]:
            lowered = name.lower()
            if lowered == b"set-cookie":
                blockers.append("Set-Cookie")
            elif lowered == b"content-encoding":
                blockers.append("Content-Encoding")
        if message.get("trailers"):
            blockers.append("trailers")
        self._status = message["status"]
        # Content-Length is recomputed on replay, so a stored value that
        # drifts from the stored body can never reach a client.
        self._headers = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in message["headers"]
            if name.lower() != b"content-length"
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
) -> bool:
    """Send a stored response, marked as a replay.

    Content-Length is recomputed from the stored body, and left off the
    statuses that carry no content, so a replay stays a valid response. A
    stored header carrying the replay name is dropped, so the marker is
    the only value under it. Reporting that is left to the caller, which
    says it once rather than once a request.

    Returns:
        Whether a stored header made way for the marker.
    """
    body = stored["body"].encode("latin-1")
    status = stored["status"]
    headers = []
    replaced = False
    for name, value in stored["headers"]:
        encoded = name.encode("latin-1")
        if encoded.lower() == replay_header:
            replaced = True
            continue
        headers.append((encoded, value.encode("latin-1")))
    if status >= _MIN_CONTENT_STATUS and status not in BODYLESS_STATUSES:
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
    headers.append((replay_header, b"true"))
    await send(
        {
            "type": "http.response.start",
            "status": stored["status"],
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": b"" if head else body})
    return replaced


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


class IdempotentRequests:
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

    def __init__(  # noqa: PLR0913
        self,
        *,
        ttl: Annotated[
            float | None,
            Doc(
                "Seconds a stored response replays for. Defaults to a day, "
                "or to `GREL_IDEMPOTENCY_TTL` where the environment sets "
                "one."
            ),
        ] = None,
        namespace: Annotated[
            str,
            Doc(
                "Namespace the stored keys sit under, so two sets of rules "
                "on one app never read each other's responses."
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
            str,
            Doc("Request header carrying the idempotency key."),
        ] = "Idempotency-Key",
        replay_header: Annotated[
            str,
            Doc(
                "Response header marking a replayed response. No standard "
                "names one, so pick what the clients already read."
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
            bool,
            Doc(
                "Answer `400` when a method in `methods` arrives without "
                "the header, instead of passing it through."
            ),
        ] = False,
        fingerprint_body: Annotated[
            bool,
            Doc(
                "Hash the request body and store the hash with the "
                "response, so a key reused with a different body gets "
                "`422` instead of a wrong replay."
            ),
        ] = False,
        max_body_size: Annotated[
            int,
            Doc("Largest body held in memory, in bytes."),
        ] = 1024 * 1024,
        wait_timeout: Annotated[
            float,
            Doc(
                "Seconds a duplicate waits for an execution already in "
                "flight, before it is answered with `409`."
            ),
        ] = 10.0,
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
        reused_status: Annotated[
            int,
            Doc(
                "Status answering a key reused with a different payload. "
                "`422` is what the Idempotency-Key header draft asks for, "
                "and `400` is what some APIs answer instead."
            ),
        ] = IDEMPOTENCY_KEY_REUSED.status,
        openapi: Annotated[
            bool,
            Doc(
                "Describe both headers and the responses the middleware "
                "returns in the OpenAPI schema. Only FastAPI builds one, "
                "and every other framework ignores this."
            ),
        ] = True,
        name: Annotated[
            str,
            Doc("Registration name, for a second set of rules on one app."),
        ] = "default",
    ) -> None:
        """Replay repeated requests through the registered cache."""
        self._name = name
        self._openapi = openapi
        self._options: dict[str, Any] = {
            "idempotency": Idempotency(namespace, ttl=ttl, cache=cache),
            "key_header": _key_name(key_header),
            "replay_header": _replay_name(replay_header),
            "methods": methods,
            "key_maker": key_maker,
            "skip": skip,
            "require_key": require_key,
            "fingerprint_body": fingerprint_body,
            "max_body_size": max_body_size,
            "wait_timeout": wait_timeout,
            "include": include,
            "exclude": exclude,
            "reused_status": reused_status,
        }

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
        return cast("Idempotency[Any]", self._options["idempotency"])

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        """Return the middleware class and the arguments to build it with.

        `micro.install(app)` reads this from every registered component
        that carries it and hands the pair to the integration, which adds
        the middleware the way its framework takes one. A component
        without it wires no middleware.
        """
        return IdempotencyMiddleware, dict(self._options)

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
