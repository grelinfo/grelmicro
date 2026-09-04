"""Tests for the structured access log."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

import httpx
import pytest
from fastapi import FastAPI
from litestar import Litestar, get
from litestar.params import Parameter
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from grelmicro import Grelmicro
from grelmicro.log import AccessLog, AccessLogMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

HTTP_OK = 200
HTTP_NOT_FOUND = 404

ACCESS_LOGGER = "grelmicro.access"
UVICORN_ACCESS_LOGGER = "uvicorn.access"


async def ok(request: object) -> PlainTextResponse:
    """Answer `200`."""
    return PlainTextResponse("ok")


async def refused(request: object) -> PlainTextResponse:
    """Answer `403`, the way an authorization layer would."""
    return PlainTextResponse("no", status_code=403)


async def boom(request: object) -> PlainTextResponse:
    """Raise, the way a handler with a bug does."""
    msg = "nope"
    raise ValueError(msg)


def starlette_app(**options: Any) -> Starlette:
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
    return [
        record for record in caplog.records if record.name == ACCESS_LOGGER
    ]


@pytest.fixture
def capture(
    caplog: pytest.LogCaptureFixture,
) -> Callable[[], list[logging.LogRecord]]:
    """Capture every access record, debug included."""
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER)
    return lambda: records(caplog)


@pytest.fixture
async def client(
    capture: Callable[[], list[logging.LogRecord]],
) -> AsyncIterator[httpx.AsyncClient]:
    """Return a client for the default middleware."""
    async with client_for(starlette_app()) as client:
        yield client


# --- What the record carries ---------------------------------------------


async def test_the_record_carries_the_request(
    client: httpx.AsyncClient,
    capture: Callable[[], list[logging.LogRecord]],
) -> None:
    """One record per request, with the fields the conventions name."""
    await client.get(
        "/orders/7?page=2", headers={"user-agent": "curl/8.4"}
    )

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
    await middleware({"type": "websocket", "path": "/ws"}, None, None)  # type: ignore[arg-type]

    assert sent and capture() == []


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
        middleware=[AccessLogMiddleware],
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


async def test_registering_it_silences_uvicorns_access_log() -> None:
    """Two access logs on one stream is worse than either alone."""
    uvicorn_access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    component = AccessLog()

    assert uvicorn_access.filter(_uvicorn_record())

    async with component:
        assert not uvicorn_access.filter(_uvicorn_record())

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
