"""Tests for the structured access log."""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import TYPE_CHECKING, Annotated, Any, cast

import httpx
import pytest
from fastapi import FastAPI
from litestar import Litestar, get
from litestar.middleware import DefineMiddleware
from litestar.params import Parameter
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from grelmicro import Grelmicro
from grelmicro.errors import MiddlewarePlacementWarning
from grelmicro.log import AccessLog, AccessLogMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

HTTP_OK = 200
HTTP_NOT_FOUND = 404

ACCESS_LOGGER = "grelmicro.access"
UVICORN_ACCESS_LOGGER = "uvicorn.access"


async def ok(_request: object) -> PlainTextResponse:
    """Answer `200`."""
    return PlainTextResponse("ok")


async def refused(_request: object) -> PlainTextResponse:
    """Answer `403`, the way an authorization layer would."""
    return PlainTextResponse("no", status_code=403)


async def boom(_request: object) -> PlainTextResponse:
    """Raise, the way a handler with a bug does."""
    msg = "nope"
    raise ValueError(msg)


def starlette_app(**options: Any) -> Starlette:  # noqa: ANN401
    """Return a Starlette app wrapped in the middleware under test."""
    app = Starlette(
        routes=[
            Route("/orders/{order_id}", ok),
            Route("/refused", refused),
            Route("/boom", boom),
            Route("/livez", ok),
            Route("/readyz", refused),
        ]
    )
    app.add_middleware(AccessLogMiddleware, **options)
    return app


def client_for(app: Any) -> httpx.AsyncClient:  # noqa: ANN401
    """Return a client that drives an ASGI app, exceptions and all."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://probe",
    )


def records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the access records captured so far."""
    return [record for record in caplog.records if record.name == ACCESS_LOGGER]


@pytest.fixture
def capture(
    caplog: pytest.LogCaptureFixture,
) -> Callable[[], list[logging.LogRecord]]:
    """Capture every access record, debug included."""
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER)
    return lambda: records(caplog)


@pytest.fixture
async def client(
    capture: Callable[[], list[logging.LogRecord]],  # noqa: ARG001
) -> AsyncIterator[httpx.AsyncClient]:
    """Return a client for the default middleware, capturing its records."""
    async with client_for(starlette_app()) as client:
        yield client


# --- What the record carries ---------------------------------------------


async def test_the_record_carries_the_request(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """One record per request, with the fields the conventions name."""
    await client.get("/orders/7?page=2", headers={"user-agent": "curl/8.4"})

    (record,) = capture()

    assert record.levelno == logging.INFO
    assert record.getMessage() == "GET /orders/7 200"
    assert record.__dict__["http.request.method"] == "GET"
    assert record.__dict__["url.path"] == "/orders/7"
    assert record.__dict__["url.query"] == "page=2"
    assert record.__dict__["url.scheme"] == "http"
    assert record.__dict__["http.response.status_code"] == HTTP_OK
    assert record.__dict__["user_agent.original"] == "curl/8.4"
    assert record.__dict__["network.protocol.version"] == "1.1"
    assert record.__dict__["http.server.request.duration"] >= 0


async def test_a_credential_in_the_query_is_redacted(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A token in a query string never reaches the sink."""
    await client.get("/orders/7?token=secret&page=2")

    (record,) = capture()

    assert record.__dict__["url.query"] == "token=***&page=2"


async def test_the_query_can_be_left_out(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A service that logs no query string at all says so once."""
    async with client_for(starlette_app(query=False)) as client:
        await client.get("/orders/7?page=2")

    assert "url.query" not in capture()[0].__dict__


async def test_the_user_agent_can_be_left_out(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """The same for the user agent."""
    async with client_for(starlette_app(user_agent=False)) as client:
        await client.get("/orders/7", headers={"user-agent": "curl/8.4"})

    assert "user_agent.original" not in capture()[0].__dict__


async def test_a_handler_that_raises_is_on_the_record(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """The exception is named, and the framework still renders it."""
    await client.get("/boom")

    (record,) = capture()

    assert record.levelno == logging.ERROR
    assert record.__dict__["error.type"] == "ValueError"
    assert record.__dict__["http.response.status_code"] == 500  # noqa: PLR2004


# --- The level follows the answer ----------------------------------------


@pytest.mark.parametrize(
    ("path", "level"),
    [
        pytest.param("/orders/7", logging.INFO, id="ok"),
        pytest.param("/refused", logging.WARNING, id="refused"),
        pytest.param("/boom", logging.ERROR, id="failed"),
        pytest.param("/livez", logging.DEBUG, id="quiet-probe"),
        pytest.param("/readyz", logging.WARNING, id="failing-probe"),
    ],
)
async def test_the_level_follows_the_answer(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
    path: str,
    level: int,
) -> None:
    """5xx is an error, 4xx a warning, a quiet path that answered a debug."""
    await client.get(path)

    assert capture()[0].levelno == level


async def test_a_probe_that_answers_is_not_read_at_info(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """At the default level a passing probe writes nothing at all.

    The record exists at debug, so it is there when someone goes looking,
    and absent from the stream a person reads.
    """
    await client.get("/livez")

    assert [
        record for record in capture() if record.levelno >= logging.INFO
    ] == []


async def test_quiet_can_be_turned_off(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A service that wants every probe logged passes `quiet=()`."""
    async with client_for(starlette_app(quiet=())) as client:
        await client.get("/livez")

    assert capture()[0].levelno == logging.INFO


# --- What it leaves alone ------------------------------------------------


async def test_exclude_writes_nothing_at_any_level(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """An excluded path leaves no record, whatever it answered."""
    async with client_for(starlette_app(exclude=("/boom",))) as client:
        await client.get("/boom")
        await client.get("/orders/7")

    assert [record.getMessage() for record in capture()] == [
        "GET /orders/7 200"
    ]


async def test_include_narrows_to_what_was_named(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """`include` logs those paths and leaves every other one alone."""
    async with client_for(starlette_app(include=("/orders/*",))) as client:
        await client.get("/orders/7")
        await client.get("/refused")

    assert [record.getMessage() for record in capture()] == [
        "GET /orders/7 200"
    ]


async def test_a_websocket_is_not_a_request_to_log(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """Only `http` scopes are access records."""
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        sent.append(scope)

    middleware = AccessLogMiddleware(app)
    await middleware(
        {"type": "websocket", "path": "/ws"}, _no_receive, _collect
    )

    assert sent
    assert capture() == []


# --- The route template, per framework -----------------------------------


async def test_fastapi_records_the_route_template(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """FastAPI carries the route it matched, so the record groups by it."""
    app = FastAPI()

    @app.get("/orders/{order_id}")
    async def order(order_id: str) -> str:
        return order_id

    app.add_middleware(AccessLogMiddleware)

    async with client_for(app) as client:
        await client.get("/orders/7")

    assert capture()[0].__dict__["http.route"] == "/orders/{order_id}"


async def test_litestar_records_the_route_template(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """Litestar writes the template into the scope under its own key."""

    @get("/orders/{order_id:str}")
    async def order(order_id: Annotated[str, Parameter()]) -> str:
        return order_id

    # `logging_config=None`: Litestar would otherwise install its own
    # logging on startup and take the capture handler with it.
    app = Litestar(
        route_handlers=[order],
        middleware=[cast("Any", AccessLogMiddleware)],
        logging_config=None,
    )

    async with client_for(app) as client:
        await client.get("/orders/7")

    assert capture()[0].__dict__["http.route"] == "/orders/{order_id}"


async def test_starlette_leaves_the_route_out(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """Starlette records no template, and a guess is worse than nothing."""
    await client.get("/orders/7")

    assert "http.route" not in capture()[0].__dict__


async def test_a_mount_is_on_both_the_path_and_the_route(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """The path the caller asked for, and the route it grouped under."""
    inner = FastAPI()

    @inner.get("/orders/{order_id}")
    async def order(order_id: str) -> str:
        return order_id

    app = Starlette(routes=[Mount("/api", app=inner)])
    app.add_middleware(AccessLogMiddleware)

    async with client_for(app) as client:
        await client.get("/api/orders/7")

    record = capture()[0]

    assert record.__dict__["url.path"] == "/api/orders/7"
    assert record.__dict__["http.route"] == "/api/orders/{order_id}"


# --- The component -------------------------------------------------------


async def test_wiring_it_silences_uvicorns_access_log() -> None:
    """Two access logs on one stream is worse than either alone."""
    uvicorn_access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    component = AccessLog()
    component.asgi_middleware()  # what `install` does for an HTTP app

    assert uvicorn_access.filter(_uvicorn_record())

    async with component:
        assert not uvicorn_access.filter(_uvicorn_record())

    assert uvicorn_access.filter(_uvicorn_record())


async def test_an_app_serving_no_http_keeps_its_access_log() -> None:
    """A FastStream app is never asked for the middleware.

    Nothing here is going to write an access record in uvicorn's place,
    so taking its access log away would leave the app with neither.
    """
    uvicorn_access = logging.getLogger(UVICORN_ACCESS_LOGGER)

    async with AccessLog():
        assert uvicorn_access.filter(_uvicorn_record())


async def test_install_adds_the_middleware(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """`micro.install(app)` wires it, so registering is the whole setup."""
    micro = Grelmicro(uses=[AccessLog()])
    app = Starlette(routes=[Route("/orders/{order_id}", ok)])
    micro.install(app)

    async with micro, client_for(app) as client:
        await client.get("/orders/7")

    assert capture()[0].getMessage() == "GET /orders/7 200"


def test_two_access_logs_are_refused() -> None:
    """One access log answers for the app, so two would write twice."""
    from grelmicro._app import ComponentAlreadyRegisteredError  # noqa: PLC0415

    with pytest.raises(ComponentAlreadyRegisteredError):
        Grelmicro(uses=[AccessLog(), AccessLog()])


def _uvicorn_record() -> logging.LogRecord:
    """Return a record shaped like one uvicorn writes."""
    return logging.LogRecord(
        UVICORN_ACCESS_LOGGER, logging.INFO, __file__, 0, "%s", ("GET",), None
    )


# --- What an outer layer answers, and what never answers at all ----------


async def test_a_request_an_outer_layer_refuses_is_still_logged(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """The refused request is the one an access log is read for.

    Authentication, CORS and a rate limiter all answer without calling
    the app, so a middleware placed behind them sees nothing at all.
    """

    class Gate:
        """An outer layer that answers `401` on its own."""

        def __init__(self, app: Any) -> None:  # noqa: ANN401
            self.app = app

        async def __call__(
            self,
            _scope: object,
            _receive: object,
            send: Callable[[dict[str, Any]], Awaitable[None]],
        ) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

    micro = Grelmicro(uses=[AccessLog()])
    app = Starlette(routes=[Route("/orders/{order_id}", ok)])
    app.add_middleware(Gate)
    micro.install(app)

    async with micro, client_for(app) as client:
        await client.get("/orders/7")

    (record,) = capture()

    assert record.levelno == logging.WARNING
    assert record.__dict__["http.response.status_code"] == 401  # noqa: PLR2004


async def test_a_cancelled_request_is_not_a_500(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A caller that hung up got no status, so none is written.

    A disconnect and a shutdown both land here, and reporting a `500` the
    caller never received would put a failure in the log for every one.
    """

    async def hangs(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        raise asyncio.CancelledError

    middleware = AccessLogMiddleware(hangs)

    with pytest.raises(asyncio.CancelledError):
        await middleware(
            {"type": "http", "method": "GET", "path": "/orders/7"},
            _no_receive,
            _collect,
        )

    (record,) = capture()

    assert record.levelno == logging.DEBUG
    assert "http.response.status_code" not in record.__dict__
    assert record.__dict__["error.type"] == "CancelledError"


async def test_a_response_that_breaks_after_its_headers_is_an_error(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A `200` that did not finish is not a success."""

    async def breaks(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        msg = "the stream stopped"
        raise RuntimeError(msg)

    middleware = AccessLogMiddleware(breaks)

    with pytest.raises(RuntimeError):
        await middleware(
            {"type": "http", "method": "GET", "path": "/orders/7"},
            _no_receive,
            _collect,
        )

    (record,) = capture()

    assert record.levelno == logging.ERROR
    assert record.__dict__["http.response.status_code"] == HTTP_OK
    assert record.__dict__["error.type"] == "RuntimeError"


async def test_the_path_is_the_one_that_arrived(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A router that rewrites the scope does not rewrite the record.

    Litestar's router replaces `path` in place, so reading it after the
    app has answered reports a path the caller never asked for.
    """

    async def rewrites(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        scope["path"] = "/rewritten"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    middleware = AccessLogMiddleware(rewrites)

    await middleware(
        {"type": "http", "method": "GET", "path": "/api/orders/7"},
        _no_receive,
        _collect,
    )

    assert capture()[0].__dict__["url.path"] == "/api/orders/7"


async def test_matching_reads_one_path_for_both_decisions(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A mount rewrites `root_path` as it goes, so it is read once.

    Matched on the way out as well as in, `exclude` and `quiet` would
    answer for two different paths on the same request.
    """

    async def mounts(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        scope["root_path"] = "/api"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    middleware = AccessLogMiddleware(mounts, exclude=("/api/livez",))

    await middleware(
        {"type": "http", "method": "GET", "path": "/api/livez"},
        _no_receive,
        _collect,
    )

    assert capture() == []


async def _collect(message: Any) -> None:  # noqa: ANN401
    """Swallow a response the test does not read."""


async def _no_receive() -> Any:  # noqa: ANN401
    """Answer the one message an app of ours would read."""
    return {"type": "http.request"}


async def test_the_resolved_caller_is_preferred_to_the_socket_peer(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """`ClientAddressMiddleware` caches the caller, and this reads it.

    Behind an ingress the socket peer is the ingress, which is what makes
    the resolved address the one an access log is read for.
    """
    from ipaddress import ip_address  # noqa: PLC0415

    from grelmicro.security import (  # noqa: PLC0415
        ClientAddress,
        ClientAddressReason,
    )

    middleware = AccessLogMiddleware(_answers(200))
    resolved = ClientAddress(
        ip=ip_address("203.0.113.9"),
        port=44320,
        reason=ClientAddressReason.RESOLVED,
        hops=1,
    )

    await middleware(
        {
            "type": "http",
            "method": "GET",
            "path": "/orders/7",
            "client": ("10.0.0.1", 5000),
            "state": {"client_address": resolved},
        },
        _no_receive,
        _collect,
    )

    assert capture()[0].__dict__["client.address"] == "203.0.113.9"


async def test_a_server_error_the_app_answered_itself_is_an_error(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """A `500` the app rendered is a failure, exception or not."""
    middleware = AccessLogMiddleware(_answers(500))

    await middleware(
        {"type": "http", "method": "GET", "path": "/orders/7"},
        _no_receive,
        _collect,
    )

    assert capture()[0].levelno == logging.ERROR


async def test_an_app_that_answers_nothing_is_written_at_debug(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """No status and no exception is not a failure to report loudly."""

    async def silent(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        return

    middleware = AccessLogMiddleware(silent)

    await middleware(
        {"type": "http", "method": "GET", "path": "/orders/7"},
        _no_receive,
        _collect,
    )

    record = capture()[0]

    assert record.levelno == logging.DEBUG
    assert "http.response.status_code" not in record.__dict__


async def test_a_record_the_level_drops_is_never_built(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe that answered costs nothing at the default level."""
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    middleware = AccessLogMiddleware(_answers(200))

    await middleware(
        {"type": "http", "method": "GET", "path": "/livez"},
        _no_receive,
        _collect,
    )

    assert records(caplog) == []


def _answers(status: int) -> Any:  # noqa: ANN401
    """Return an app that answers `status` with an empty body."""

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    return app


# --- What a mistake costs -------------------------------------------------


@pytest.mark.parametrize("field", ["include", "exclude", "quiet"])
def test_a_bare_string_of_patterns_is_refused(field: str) -> None:
    """A missing comma would turn the whole access log off, silently.

    A string is a sequence of characters, so `exclude="/internal/*"` is
    walked one character at a time, and the `*` matches every path as a
    prefix.
    """
    # Cast because the annotation already says tuple: the guard is for the
    # caller who passes a string anyway, which a checker does not always
    # see, and which fails silently without it.
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        AccessLogMiddleware(_answers(200), **mistake)

    with pytest.raises(TypeError, match="is a string"):
        AccessLog(**mistake)


async def test_install_on_litestar_warns_about_nothing(
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """Wrapping the whole stack is where a watcher belongs.

    Litestar builds its middleware at construction, so `install` can only
    wrap the app whole. For a middleware that answers requests that is
    worth a warning. For one that only watches it is the right place, and
    a warning would send the reader to move it inside, where a request an
    outer layer refuses would never reach it.
    """

    @get("/orders")
    async def orders() -> str:
        return "ok"

    class Gate:
        """An outer layer that answers `401` on its own."""

        def __init__(self, app: Any) -> None:  # noqa: ANN401
            self.app = app

        async def __call__(
            self,
            _scope: object,
            _receive: object,
            send: Callable[[dict[str, Any]], Awaitable[None]],
        ) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

    app = Litestar(
        route_handlers=[orders],
        middleware=[DefineMiddleware(cast("Any", Gate))],
        logging_config=None,
    )
    micro = Grelmicro(uses=[AccessLog()])

    with warnings.catch_warnings():
        warnings.simplefilter("error", MiddlewarePlacementWarning)
        micro.install(app)

    async with micro, client_for(app) as client:
        await client.get("/orders")

    assert capture()[0].__dict__["http.response.status_code"] == 401  # noqa: PLR2004
