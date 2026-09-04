"""Endpoints grelmicro serves itself, without a web framework.

One tiny ASGI app builder, shared by the health endpoints, the metrics
endpoint, and `OpsServer`. It answers what a probe needs and nothing more: a
table of exact paths, `GET` and `HEAD`, and a body the caller rendered.

It is not a framework. There is no routing beyond an exact match, no path
parameter, and no request body, because nothing grelmicro serves on its own
needs any of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import parse_qs

from grelmicro._paths import route_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
    Handler = Callable[[Scope], Awaitable["Rendered"]]

HTTP_OK: Final = 200
HTTP_NOT_FOUND: Final = 404
HTTP_METHOD_NOT_ALLOWED: Final = 405
HTTP_SERVICE_UNAVAILABLE: Final = 503

SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
"""What an endpoint of ours answers. Everything else is a `405`."""

NO_STORE_HEADERS: Final = {"Cache-Control": "no-store"}
"""What every answer carries, for a door that writes headers by name."""

_GOING_AWAY: Final = 1001
"""Websocket close code: this endpoint is not one to connect to."""

ALLOW_HEADER: Final = ((b"allow", b"GET, HEAD"),)
"""What a `405` carries, so a caller reads which methods it may use."""
_NO_STORE: Final = (b"cache-control", b"no-store")


class Rendered(NamedTuple):
    """One answer, rendered. What every door writes, whatever writes it."""

    status: int
    body: bytes
    media_type: str | None = None


def query_value(scope: Scope, name: str) -> str | None:
    """Return the value of a query parameter, or `None`.

    The last one when it is repeated, which is what Starlette hands a
    handler declaring a single string, and therefore what the FastAPI
    router answers for the same request.
    """
    raw = scope.get("query_string") or b""
    if not raw:
        return None
    values = parse_qs(
        raw.decode("latin-1"), keep_blank_values=True, errors="replace"
    ).get(name)
    return values[-1] if values else None


def response_headers(rendered: Rendered) -> list[tuple[bytes, bytes]]:
    """Build the headers of a rendered answer.

    `Cache-Control: no-store` on every one, because a probe answer and a
    metrics scrape both describe this instant. `Content-Type` only when
    there is a body to type, so an empty probe response carries none.
    """
    headers = [
        _NO_STORE,
        (b"content-length", str(len(rendered.body)).encode("latin-1")),
    ]
    if rendered.media_type is not None:
        headers.append((b"content-type", rendered.media_type.encode("latin-1")))
    return headers


def normalize(path: str) -> str:
    """Return the path in the one shape the route table is keyed by.

    One leading slash, no trailing one, so a mount that hands back
    `livez/` and a request for `/livez` reach the same handler.
    """
    trimmed = path.strip("/")
    return f"/{trimmed}"


def check_prefix(prefix: str) -> None:
    """Refuse a prefix no request can match.

    A trailing slash keys the table one segment deeper than any request
    normalizes to, so every path would answer `404` with nothing to read
    from. FastAPI refuses the same input on a router.

    Raises:
        ValueError: If `prefix` ends with a slash.
    """
    if prefix.endswith("/"):
        msg = (
            f"prefix={prefix!r} must not end with '/', because no request "
            f"would match it. Write it as {prefix.rstrip('/')!r}."
        )
        raise ValueError(msg)


def build_asgi(routes: Mapping[str, Handler]) -> ASGIApp:
    """Build a pure-ASGI app serving `routes`, keyed by exact path.

    A path is matched the way every grelmicro middleware matches one,
    through `route_path`, and then normalized, so one app answers the same
    paths wherever it is mounted. Frameworks hand a mounted app the leftover
    path differently: Starlette leaves it whole beside a `root_path`,
    Litestar hands the remainder, with a trailing slash and sometimes
    without the leading one. `/livez` is the same probe through all of them.
    """
    matched = {normalize(path): handler for path, handler in routes.items()}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        """Answer one request, or complete the lifespan protocol."""
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await _lifespan(receive, send)
            return
        if scope_type == "websocket":
            # Mounted at the root, a websocket to any path reaches here.
            # Returning without a word leaves the server to report a
            # handshake that never came.
            await receive()
            await send({"type": "websocket.close", "code": _GOING_AWAY})
            return
        if scope_type != "http":
            return
        handler = matched.get(normalize(route_path(scope)))
        extra: tuple[tuple[bytes, bytes], ...] = ()
        if handler is None:
            rendered = Rendered(HTTP_NOT_FOUND, b"")
        elif scope["method"] not in SAFE_METHODS:
            rendered = Rendered(HTTP_METHOD_NOT_ALLOWED, b"")
            extra = ALLOW_HEADER
        else:
            rendered = await handler(scope)
        await send(
            {
                "type": "http.response.start",
                "status": rendered.status,
                "headers": [*response_headers(rendered), *extra],
            }
        )
        await send({"type": "http.response.body", "body": rendered.body})

    return app


async def _lifespan(receive: Receive, send: Send) -> None:
    """Answer the lifespan protocol, so the app can be served on its own.

    It owns no resource: the components it reads are opened by the
    `Grelmicro` app, wherever that app is opened.
    """
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        else:
            await send({"type": "lifespan.shutdown.complete"})
            return
