"""Starlette integration: binding, error responses, and idempotency.

Everything here is pure ASGI, so it works on a plain Starlette app and on
anything built from one. `grelmicro.integrations.fastapi` builds on it and
adds what only FastAPI has, an OpenAPI schema and a health router.
"""

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast

from typing_extensions import Doc

from grelmicro._app import AmbientBindingError
from grelmicro.errors import OutOfContextError
from grelmicro.http import ErrorResponses, merge_headers, send_error
from grelmicro.http._kinds import (
    _IN_FLIGHT_RETRY_AFTER,
    HANDLED,
    IDEMPOTENCY_IN_FLIGHT,
    IDEMPOTENCY_KEY_INVALID,
    IDEMPOTENCY_KEY_REUSED,
    REQUEST_BODY_TOO_LARGE,
    Kind,
    Occurrence,
)
from grelmicro.idempotency.errors import (
    IdempotencyConflictError,
    IdempotencyKeyMakerError,
    IdempotencyWaitTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Collection,
        MutableMapping,
        Sequence,
    )

    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import Response

    from grelmicro import Grelmicro
    from grelmicro.idempotency import Idempotency

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = [
    "GrelmicroMiddleware",
    "IdempotencyMiddleware",
    "StoredResponse",
    "error_response",
    "install",
    "install_error_responses",
    "is_bound",
]


def install(
    app: Annotated[
        "Starlette",
        Doc("The Starlette application to wire."),
    ],
    micro: Annotated[
        "Grelmicro",
        Doc(
            "The `Grelmicro` app to open in the lifespan and bind per request."
        ),
    ],
    *,
    ambient: Annotated[
        bool,
        Doc(
            "Add `GrelmicroMiddleware` so patterns resolve ambiently inside "
            "request handlers. Default `True`. Pass `False` to skip it."
        ),
    ] = True,
) -> None:
    """Wire `micro` into a Starlette app.

    Chains `async with micro:` around the app's existing lifespan, so any
    lifespan already passed to the framework keeps running and the components
    are open before the first request. When `ambient` is `True`, adds
    `GrelmicroMiddleware` so patterns resolve through `Grelmicro.current()`
    inside request handlers, and keeps it outside every other middleware
    however they were added, so one that resolves a backend ambiently, such
    as `IdempotencyMiddleware`, always runs inside the request scope. The
    placement is read back on startup and raises `AmbientBindingError` if it
    did not hold.

    Prefer the polymorphic `micro.install(app)`, which detects the framework
    and calls this for you.
    """
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: "Starlette") -> "AsyncIterator[Any]":
        _check_binding_outermost(app)
        async with previous(app) as state, micro:
            yield state

    app.router.lifespan_context = lifespan
    if ambient:
        app.add_middleware(GrelmicroMiddleware, micro=micro)
        _keep_binding_outermost(app)
    else:
        micro._on_ambient_disabled()  # noqa: SLF001


def install_error_responses(
    app: Annotated[
        "Starlette",
        Doc("The Starlette application to wire."),
    ],
    errors: Annotated[
        ErrorResponses,
        Doc("The registered component that renders each rejection."),
    ],
) -> None:
    """Render grelmicro rejections in a standard format on a Starlette app.

    Registers one exception handler per rejection grelmicro raises to turn a
    caller away, and reshapes the framework's own errors into the same
    format, so the whole API answers in one shape.

    Starlette looks a handler up through the raised exception's class
    hierarchy, so registering `AdmissionError` covers every rejection under
    it, including one a later release adds.

    `micro.install(app)` calls this when `ErrorResponses()` is registered.
    Call it directly only on an app that never goes through `install`.

    Read more in the [Error Responses](../http/errors.md) docs.
    """
    # Recorded so `error_response` reads the format the app actually
    # answers in, rather than assuming RFC 9457.
    app.state.grelmicro_error_responses = errors

    async def handler(request: "Request", exc: Exception) -> "Response":
        """Render one rejection, taking the occurrence from the request path."""
        from starlette.responses import Response  # noqa: PLC0415

        rendered = errors.render(exc, instance=request.url.path)
        if rendered is None:  # pragma: no cover
            raise exc
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    async def http_error(request: "Request", exc: Exception) -> "Response":
        """Reshape the framework's own error into the registered format."""
        from starlette.responses import Response  # noqa: PLC0415

        http_exc = cast("HTTPException", exc)
        headers = http_exc.headers or {}
        if http_exc.status_code in _BODYLESS_STATUSES:
            # A `204` or a `304` carries no body by the protocol, whatever
            # format the app answers in. Starlette guards this in its own
            # handler and so must this one.
            return Response(status_code=http_exc.status_code, headers=headers)
        detail = http_exc.detail
        rendered = errors.render_status(
            http_exc.status_code,
            detail=detail if isinstance(detail, str) else None,
            extensions=_structured_detail(detail),
            instance=request.url.path,
        )
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=merge_headers(rendered, headers),
        )

    async def validation_error(
        request: "Request",
        exc: Exception,
    ) -> "Response":
        """Reshape a request that did not match the endpoint's shape."""
        from starlette.responses import Response  # noqa: PLC0415

        rendered = errors.render_validation(
            _field_errors(exc),
            status=HTTP_422_UNPROCESSABLE_CONTENT,
            instance=request.url.path,
        )
        return Response(
            content=rendered.body,
            status_code=rendered.status,
            media_type=rendered.media_type,
            headers=rendered.headers,
        )

    _install_framework_handlers(app, http_error, validation_error)

    for klass in HANDLED:
        # A handler the app registered first wins. Registering before
        # `install` is the natural order (build the app, register handlers,
        # wire grelmicro), and overwriting there would take a handler away
        # from an app that upgrades, without saying so.
        if klass not in app.exception_handlers:
            app.add_exception_handler(klass, handler)


_logger = logging.getLogger(__name__)


HTTP_422_UNPROCESSABLE_CONTENT = 422
"""Status FastAPI answers a request that failed validation with."""


class GrelmicroMiddleware:
    """Bind the active `Grelmicro` app for the duration of each request.

    A request handler runs in its own task, outside the `async with micro:`
    block, so `Grelmicro.current()` and the ambient `backend=` resolution it
    powers do not see the app there. This middleware sets the active app for
    the request task, so `Lock("cart")`, `RateLimiter.sliding_window(...)`,
    and `@cached` resolve ambiently inside the handler exactly as they do in
    a task.

    ```python
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.integrations.fastapi import GrelmicroMiddleware

    micro = Grelmicro(uses=[...])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with micro:
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(GrelmicroMiddleware, micro=micro)
    ```

    Open the app in the framework lifespan so its components are registered
    before any request arrives. The middleware is pure ASGI and works with
    any ASGI framework (Starlette, Litestar, ...). It binds on `http` and
    `websocket` scopes and passes the `lifespan` scope through untouched.
    """

    def __init__(
        self,
        app: Annotated[
            "ASGIApp",
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        micro: Annotated[
            "Grelmicro",
            Doc(
                "The `Grelmicro` app to bind for each request. Open it in "
                "the framework lifespan so its components are ready."
            ),
        ],
    ) -> None:
        """Initialize the middleware with the app to bind."""
        self.app = app
        self.micro = micro

    async def __call__(
        self, scope: "Scope", receive: "Receive", send: "Send"
    ) -> None:
        """Bind the app on request scopes, pass other scopes through."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = self.micro._bind_current()  # noqa: SLF001
        try:
            await self.app(scope, receive, send)
        finally:
            self.micro._reset_current(token)  # noqa: SLF001


def _is_binding(middleware: object) -> bool:
    """Return whether a `user_middleware` entry is `GrelmicroMiddleware`.

    Matches the class itself and not a subclass, so only a middleware whose
    whole body is the binding is moved.
    """
    return getattr(middleware, "cls", None) is GrelmicroMiddleware


def _check_binding_outermost(app: "Starlette") -> None:
    """Raise when a registered `GrelmicroMiddleware` is not the outermost one.

    Runs once the stack is built. Reading the order back turns a placement
    that did not hold into a failure at startup, rather than an
    `OutOfContextError` on the first request that resolves a backend.
    """
    binding = [
        index
        for index, middleware in enumerate(app.user_middleware)
        if _is_binding(middleware)
    ]
    if binding and binding[0] != 0:
        outer = getattr(app.user_middleware[0].cls, "__name__", "A middleware")
        msg = (
            f"{outer} wraps GrelmicroMiddleware, so a middleware that "
            f"resolves a backend ambiently, such as IdempotencyMiddleware, "
            f"raises OutOfContextError on every request that reaches it. "
            f"Add GrelmicroMiddleware last, or let micro.install(app) "
            f"place it."
        )
        raise AmbientBindingError(msg)


def _keep_binding_outermost(app: "Starlette") -> None:
    """Move `GrelmicroMiddleware` to the front of the stack as it is built.

    The framework builds its middleware stack once, on the first request or
    lifespan event, from `app.user_middleware`. Reordering that list at build
    time puts the binding middleware outside every other one, so a middleware
    added after `install` still runs inside the grelmicro request scope.
    """
    build = app.build_middleware_stack

    def build_middleware_stack() -> "ASGIApp":
        current = app.user_middleware
        app.user_middleware = [
            *(m for m in current if _is_binding(m)),
            *(m for m in current if not _is_binding(m)),
        ]
        return build()

    app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]


_MAX_KEY_LENGTH = 255
"""Longest accepted idempotency key, in characters.

A longer key is answered with `400`.
"""


_REPLAY_HEADER = b"idempotent-replayed"
"""Response header marking a replayed response."""


_MIN_CONTENT_STATUS = 200
"""Lowest status that may carry content."""


_BODYLESS_STATUSES = frozenset({204, 304})
"""Statuses that carry no content, so a replay sends no Content-Length."""


_KEY_SEPARATOR = "\x1f"
"""Separator joining the parts of a stored key."""


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
    headers: "Sequence[Sequence[str]]"
    body: str


class IdempotencyMiddleware:
    """Replay a stored HTTP response when a request repeats its idempotency key.

    A request whose method is listed in `methods` and which carries the
    `header` runs once. A retry with the same key replays the stored
    status, headers, and body without reaching the handler, and carries
    `idempotent-replayed: true`. A request without the header passes
    straight through, so adding the middleware changes nothing until a
    client opts in.

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.idempotency import Idempotency
    from grelmicro.integrations.fastapi import IdempotencyMiddleware

    micro = Grelmicro(uses=[...])
    app = FastAPI()
    micro.install(app)

    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=3600)
    )
    ```

    Add it before or after `micro.install(app)`. It resolves its `Cache`
    through the grelmicro request scope, which `install` keeps outside
    every other middleware.

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

    def __init__(
        self,
        app: Annotated[
            "ASGIApp",
            Doc("The next ASGI application in the middleware chain."),
        ],
        *,
        idempotency: Annotated[
            "Idempotency[Any]",
            Doc(
                "The `Idempotency` that stores responses. Its `ttl` sets "
                "how long a key replays."
            ),
        ],
        header: Annotated[
            str,
            Doc("Request header carrying the idempotency key."),
        ] = "Idempotency-Key",
        methods: Annotated[
            "Collection[str]",
            Doc(
                "Methods that take an idempotency key. Every other method "
                "passes through."
            ),
        ] = ("POST",),
        key_maker: Annotated[
            "Callable[[Scope, str], str] | None",
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
            "Callable[[StoredResponse], bool] | None",
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
    ) -> None:
        """Initialize the middleware with the idempotency store and policy."""
        self.app = app
        self._idempotency = idempotency
        self._header = header.lower().encode("latin-1")
        self._header_name = header
        self._methods = frozenset(method.upper() for method in methods)
        self._key_maker = key_maker
        self._skip = skip
        self._require_key = require_key
        self._fingerprint_body = fingerprint_body
        self._max_body_size = max_body_size
        self._wait_timeout = wait_timeout

    async def __call__(
        self, scope: "Scope", receive: "Receive", send: "Send"
    ) -> None:
        """Replay, execute, or pass the request through."""
        if scope["type"] != "http" or scope["method"] not in self._methods:
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
        scope: "Scope",
        receive: "Receive",
        send: "Send",
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
                IDEMPOTENCY_KEY_REUSED,
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
                await _send_stored(
                    send, operation.result(), head=scope["method"] == "HEAD"
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

    def _storage_key(self, scope: "Scope", key: str) -> str:
        """Build the stored key, scoped by route unless `key_maker` says otherwise."""
        if self._key_maker is not None:
            return _checked_key(self._key_maker(scope, key), key)
        parts = [scope["method"], scope["path"]]
        query = scope.get("query_string", b"")
        if query:
            parts.append(query.decode("latin-1"))
        parts.append(key)
        return _KEY_SEPARATOR.join(parts)


def error_response(
    request: Annotated[
        "Request",
        Doc("The request being answered, which knows its app."),
    ],
    *,
    status: Annotated[int, Doc("HTTP status code of the response.")],
    detail: Annotated[
        str | None,
        Doc("Explanation of this occurrence, safe to show a client."),
    ] = None,
    extensions: Annotated[
        dict[str, Any] | None,
        Doc("Extra members to carry, where the format has room for them."),
    ] = None,
) -> "Response":
    """Answer from your own exception handler in the app's error format.

    Writing a handler of your own is how one error opts out of the shape
    grelmicro installs. This is for the other case: you want your own
    handler and the same shape as everything else.

    ```python
    from grelmicro.integrations.fastapi import error_response


    @app.exception_handler(InsufficientFunds)
    async def handle(request: Request, exc: InsufficientFunds) -> Response:
        return error_response(
            request,
            status=409,
            detail="The account does not hold enough to cover this charge.",
            extensions={"balance": exc.balance},
        )
    ```

    The format is read from the app, so a service that registered
    `ErrorResponses.tmf()` answers in TMF from here too, with no second
    place to keep in step. An app that registered nothing gets RFC 9457.
    """
    from starlette.responses import Response  # noqa: PLC0415

    errors = (
        getattr(request.app.state, "grelmicro_error_responses", None)
        or ErrorResponses()
    )
    rendered = errors.render_status(
        status,
        detail=detail,
        instance=request.url.path,
        extensions=extensions,
    )
    return Response(
        content=rendered.body,
        status_code=rendered.status,
        media_type=rendered.media_type,
        headers=rendered.headers,
    )


def _framework_errors() -> "list[tuple[type[Exception], bool]]":
    """Return the framework's own errors, paired with whether they validate.

    Registering these is what makes the whole API answer in one shape. An
    `HTTPException` a handler raises and a request that failed validation
    are errors the client caused, so they belong in the same format as a
    rejection.
    """
    from starlette.exceptions import HTTPException  # noqa: PLC0415

    errors: list[tuple[type[Exception], bool]] = [(HTTPException, False)]
    try:
        from fastapi.exceptions import (  # noqa: PLC0415
            RequestValidationError,
        )
    except ImportError:  # pragma: no cover
        # A plain Starlette app validates nothing, so there is no such
        # error to reshape.
        return errors
    errors.append((RequestValidationError, True))
    return errors


def _is_framework_default(app: "Starlette", klass: type[Exception]) -> bool:
    """Return whether the handler for `klass` is the one FastAPI installed.

    FastAPI registers its own handlers in the constructor, so "already
    registered" cannot mean "the app chose this". Only a handler the app
    put there is left alone, which is how it opts one error back out of the
    shared format.

    A plain Starlette app registers nothing, so a missing entry is the same
    answer as a default one.
    """
    registered = app.exception_handlers.get(klass)
    if registered is None:
        return True
    try:
        from fastapi.exception_handlers import (  # noqa: PLC0415
            http_exception_handler,
            request_validation_exception_handler,
        )
    except ImportError:  # pragma: no cover
        return False
    return registered in (
        http_exception_handler,
        request_validation_exception_handler,
    )


def _field_errors(exc: Exception) -> "list[dict[str, Any]]":
    """Return the field errors of a validation failure, without the input.

    `loc`, `msg` and `type` tell a client which part of the request to fix.
    `input` only repeats what the client just sent, and `ctx` carries an
    exception object that serializes to nothing useful. Both are dropped.
    """
    raw = getattr(exc, "errors", None)
    entries = raw() if callable(raw) else []
    return [
        {key: value for key, value in entry.items() if key in _FIELD_KEYS}
        for entry in entries
    ]


_FIELD_KEYS = frozenset({"loc", "msg", "type"})
"""Members of a field error that help a client fix its request."""


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
    if not isinstance(built, str) or not built:
        msg = f"key_maker returned {built!r}, expected a non-empty string."
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
        send: "Send",
        max_body_size: int,
        skip: "Callable[[StoredResponse], bool] | None" = None,
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

    async def __call__(self, message: "Message") -> None:
        """Capture the message, then forward it downstream."""
        if message["type"] == "http.response.start":
            self._start(message)
        elif message["type"] == "http.response.body":
            self._body(message)
        await self._send(message)

    def _start(self, message: "Message") -> None:
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

    def _body(self, message: "Message") -> None:
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
    headers: "Sequence[tuple[bytes, bytes]]", name: bytes
) -> str | None:
    """Return the first value of `name`, or None when it is absent."""
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1").strip()
    return None


async def _buffer_request(
    receive: "Receive", max_body_size: int
) -> "tuple[bytes | None, bool, Receive]":
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

    async def replay_receive() -> "Message":
        message = next(pending, None)
        if message is None:
            return await receive()
        return message

    return (b"".join(chunks) if complete else None), False, replay_receive


async def _send_stored(send: "Send", stored: _Entry, *, head: bool) -> None:
    """Send a stored response, marked as a replay.

    Content-Length is recomputed from the stored body, and left off the
    statuses that carry no content, so a replay stays a valid response.
    """
    body = stored["body"].encode("latin-1")
    status = stored["status"]
    headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in stored["headers"]
    ]
    if status >= _MIN_CONTENT_STATUS and status not in _BODYLESS_STATUSES:
        headers.append((b"content-length", str(len(body)).encode("latin-1")))
    headers.append((_REPLAY_HEADER, b"true"))
    await send(
        {
            "type": "http.response.start",
            "status": stored["status"],
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": b"" if head else body})


async def _refuse(
    send: "Send",
    scope: "Scope",
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


def _install_framework_handlers(
    app: "Starlette",
    http_error: "Callable[..., Any]",
    validation_error: "Callable[..., Any]",
) -> None:
    """Reshape the framework's own errors into the registered format.

    A handler the app registered itself is left alone. The one grelmicro
    installs for validation is recorded, so anything that documents the
    schema can tell whether grelmicro is still the one answering.
    """
    for klass, validates in _framework_errors():
        if not _is_framework_default(app, klass):
            continue
        handler = validation_error if validates else http_error
        app.add_exception_handler(klass, handler)
        if validates:
            app.state.grelmicro_validation_handler = handler


def _structured_detail(detail: Any) -> "dict[str, Any] | None":  # noqa: ANN401
    """Return the extension members a non-string `detail` becomes.

    FastAPI documents a dict or a list there, and dropping it would lose a
    payload the app meant the client to read.

    A mapping is what an extension member already is, so its entries are
    carried as members in their own right. Anything else goes under
    `errors`, the name RFC 9457 readers expect a list of sub-problems
    under. A name that would displace one of the five standard members is
    dropped by `build`, so merging is safe.
    """
    if detail is None or isinstance(detail, str):
        return None
    if isinstance(detail, dict):
        return dict(detail)
    return {"errors": detail}


def _registered_errors(scope: "Scope") -> ErrorResponses:
    """Return the app's `ErrorResponses`, or the default when none is set."""
    app = scope.get("app")
    registered = getattr(
        getattr(app, "state", None), "grelmicro_error_responses", None
    )
    return registered if registered is not None else ErrorResponses()


def is_bound(
    app: Annotated[
        "Starlette",
        Doc("The Starlette or FastAPI application to inspect."),
    ],
) -> bool:
    """Return whether `install` added the per-request binding middleware.

    Called by `Grelmicro.check_ambient_binding` and `Grelmicro.describe` to
    catch an app that never had `micro.install(app)` called on it, including
    a mounted sub-application, which otherwise resolves against the host's
    components with nothing reporting it.
    """
    return any(
        getattr(middleware, "cls", None) is GrelmicroMiddleware
        for middleware in getattr(app, "user_middleware", ())
    )
