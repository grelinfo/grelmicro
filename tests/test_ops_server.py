"""Tests for the ops server, over a real socket.

Every test here talks to a listening port, because the point of `OpsServer`
is that a process running no web framework still answers an orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.http import OpsServer, OpsServerConfig, OpsServerError
from grelmicro.http._server import _Answer, _call_app
from grelmicro.metrics import Metrics, MetricsConfig, MetricsExporterType
from tests.health.conftest import (
    healthy,
    healthy_with_details,
    slow,
    unhealthy,
)
from tools.demo_port import free_port

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_REQUEST_TIMEOUT = 408
HTTP_CONTENT_TOO_LARGE = 413
HTTP_HEADERS_TOO_LARGE = 431
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_NOT_IMPLEMENTED = 501

pytestmark = [pytest.mark.timeout(30)]


# --- Helpers -------------------------------------------------------------


async def raw(port: int, request: bytes, *, eof: bool = True) -> bytes:
    """Send bytes to the port and read everything it answers.

    `eof=False` holds the connection open after the bytes, which is what a
    client that stopped mid-request does.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        if eof:
            writer.write_eof()
        return await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()


def status_of(answer: bytes) -> int:
    """Return the status code of a raw HTTP answer."""
    return int(answer.split(b" ", 2)[1])


@pytest.fixture
def port() -> int:
    """Return a port free on both loopback families."""
    return free_port()


@pytest.fixture
def health() -> HealthChecks:
    """Return a health component with caching off and one passing check."""
    checks = HealthChecks(cache_ttl=0)
    checks.add("db", healthy())
    return checks


@pytest.fixture
async def serving(health: HealthChecks, port: int) -> AsyncIterator[Grelmicro]:
    """Open an app serving the health endpoints on a loopback port."""
    micro = Grelmicro(uses=[health, OpsServer(host="127.0.0.1", port=port)])
    async with micro:
        yield micro


@pytest.fixture
async def client(
    serving: Grelmicro,  # noqa: ARG001
    port: int,
) -> AsyncIterator[httpx.AsyncClient]:
    """Return a client pointed at the serving port."""
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        yield client


# --- What it serves ------------------------------------------------------


async def test_probes_answer_over_the_socket(
    client: httpx.AsyncClient,
) -> None:
    """The three health endpoints answer on the port, with no framework."""
    assert (await client.get("/livez")).status_code == HTTP_200_OK
    assert (await client.get("/readyz")).status_code == HTTP_200_OK

    response = await client.get("/healthz")

    assert response.status_code == HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "checks": {"db": {"status": "ok", "critical": True}},
    }


async def test_a_failing_critical_check_answers_503(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """Readiness reports the aggregate the same way every door does."""
    health.add("cache", unhealthy())

    assert (
        await client.get("/readyz")
    ).status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert (
        await client.get("/healthz")
    ).status_code == HTTP_503_SERVICE_UNAVAILABLE


async def test_exclude_reaches_the_component(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """The query string is parsed on this door too."""
    health.add("flaky", unhealthy())

    response = await client.get("/readyz?exclude=flaky")

    assert response.status_code == HTTP_200_OK


async def test_head_answers_without_a_body(
    client: httpx.AsyncClient,
) -> None:
    """A HEAD carries the length a GET would, and no bytes."""
    head = await client.head("/healthz")
    get = await client.get("/healthz")

    assert head.status_code == get.status_code
    assert head.headers["content-length"] == str(len(get.content))
    assert head.content == b""


async def test_unknown_path_and_method_are_refused(
    client: httpx.AsyncClient,
) -> None:
    """It serves its own endpoints and nothing else."""
    assert (await client.get("/")).status_code == HTTP_NOT_FOUND

    response = await client.post("/livez")

    assert response.status_code == HTTP_METHOD_NOT_ALLOWED
    assert response.headers["allow"] == "GET, HEAD"


@pytest.mark.usefixtures("serving")
async def test_the_connection_closes_after_one_answer(port: int) -> None:
    """One request per connection, and the answer says so."""
    answer = await raw(port, b"GET /livez HTTP/1.1\r\nhost: probe\r\n\r\n")

    assert b"connection: close" in answer
    assert b"date: " in answer


async def test_metrics_is_served_when_registered(
    health: HealthChecks, port: int
) -> None:
    """A worker scraped by Prometheus gets its endpoint on the same port."""
    micro = Grelmicro(
        uses=[
            health,
            Metrics(exporter=MetricsExporterType.PROMETHEUS),
            OpsServer(host="127.0.0.1", port=port),
        ]
    )
    async with micro:
        micro.metrics.counter("orders.placed", unit="1").add(1)
        assert micro.get("ops", "default").paths == (
            "/livez",
            "/readyz",
            "/healthz",
            "/metrics",
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}"
        ) as client:
            response = await client.get("/metrics")

    assert response.status_code == HTTP_200_OK
    assert "orders_placed" in response.text


async def test_metrics_alone_is_enough(port: int) -> None:
    """An app with metrics and no health checks serves the scrape target."""
    micro = Grelmicro(
        uses=[
            Metrics(exporter=MetricsExporterType.PROMETHEUS),
            OpsServer(host="127.0.0.1", port=port),
        ]
    )
    async with micro:
        assert micro.get("ops", "default").paths == ("/metrics",)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}"
        ) as client:
            assert (await client.get("/metrics")).status_code == HTTP_200_OK
            assert (await client.get("/livez")).status_code == HTTP_NOT_FOUND


async def test_a_check_resolves_the_app_ambiently(port: int) -> None:
    """The request task carries the app, as a framework's request task does."""
    health = HealthChecks(cache_ttl=0)
    seen: list[str] = []

    async def check() -> None:
        seen.append(Grelmicro.current().get("health", "default").name)

    health.add("ambient", check)
    micro = Grelmicro(uses=[health, OpsServer(host="127.0.0.1", port=port)])
    async with (
        micro,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
    ):
        response = await client.get("/readyz")

    assert response.status_code == HTTP_200_OK
    assert seen == ["default"]


async def test_show_details_reaches_the_report(port: int) -> None:
    """The server's own `show_details` decides what /healthz carries."""
    health = HealthChecks(cache_ttl=0)
    health.add("redis", healthy_with_details({"version": "7.2"}))
    micro = Grelmicro(
        uses=[
            health,
            OpsServer(host="127.0.0.1", port=port, show_details=True),
        ]
    )
    async with (
        micro,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
    ):
        response = await client.get("/healthz")

    assert response.json()["checks"]["redis"]["details"] == {"version": "7.2"}


# --- What it refuses to start on -----------------------------------------


async def test_an_app_with_nothing_to_serve_is_refused(port: int) -> None:
    """A server with no endpoint says so instead of listening."""
    micro = Grelmicro(uses=[OpsServer(host="127.0.0.1", port=port)])

    with pytest.raises(OpsServerError, match="nothing to serve"):
        async with micro:
            pass  # pragma: no cover


async def test_outside_an_app_is_refused(port: int) -> None:
    """It serves what an app registers, so it needs one."""
    server = OpsServer(host="127.0.0.1", port=port)

    with pytest.raises(OpsServerError, match="registered on one"):
        async with server:
            pass  # pragma: no cover


@pytest.mark.usefixtures("serving")
async def test_a_taken_port_is_refused(health: HealthChecks, port: int) -> None:
    """The second bind names the address it could not have."""
    micro = Grelmicro(uses=[health, OpsServer(host="127.0.0.1", port=port)])

    with pytest.raises(OpsServerError, match=f"127.0.0.1:{port}"):
        async with micro:
            pass  # pragma: no cover


# --- What it does with a request that is not one -------------------------


@pytest.mark.parametrize(
    ("request_bytes", "expected"),
    [
        pytest.param(b"nonsense\r\n\r\n", HTTP_BAD_REQUEST, id="request-line"),
        pytest.param(
            b"GET /livez HTTP/1.1\r\nbroken\r\n\r\n",
            HTTP_BAD_REQUEST,
            id="header-without-colon",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\ncontent-length: nine\r\n\r\n",
            HTTP_BAD_REQUEST,
            id="length-not-a-number",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\ncontent-length: -1\r\n\r\n",
            HTTP_BAD_REQUEST,
            id="negative-length",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\ncontent-length: 99999\r\n\r\n",
            HTTP_CONTENT_TOO_LARGE,
            id="body-too-large",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\ntransfer-encoding: chunked\r\n\r\n",
            HTTP_NOT_IMPLEMENTED,
            id="chunked",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\nx: " + b"a" * 9000 + b"\r\n\r\n",
            HTTP_HEADERS_TOO_LARGE,
            id="header-too-long",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\n"
            + b"".join(b"x-%d: 1\r\n" % index for index in range(70))
            + b"\r\n",
            HTTP_HEADERS_TOO_LARGE,
            id="too-many-headers",
        ),
        pytest.param(
            b"GET /livez HTTP/1.1\r\nhost: probe\r\n",
            HTTP_BAD_REQUEST,
            id="headers-never-end",
        ),
    ],
)
@pytest.mark.usefixtures("serving")
async def test_a_request_it_cannot_read_is_refused(
    port: int, request_bytes: bytes, expected: int
) -> None:
    """Each malformed request gets the status that names what went wrong."""
    answer = await raw(port, request_bytes)

    assert status_of(answer) == expected


@pytest.mark.usefixtures("serving")
async def test_a_body_it_can_drop_is_answered(port: int) -> None:
    """A small body is read and discarded, and the request still answers."""
    answer = await raw(
        port,
        b"GET /livez HTTP/1.1\r\ncontent-length: 2\r\n\r\nhi",
    )

    assert status_of(answer) == HTTP_200_OK


async def test_a_connection_that_sends_nothing_is_dropped(
    port: int, client: httpx.AsyncClient
) -> None:
    """A TCP check of the port gets no answer, and the server stays up."""
    answer = await raw(port, b"")

    assert answer == b""
    assert (await client.get("/livez")).status_code == HTTP_200_OK


async def test_a_request_that_never_finishes_times_out(
    health: HealthChecks, port: int
) -> None:
    """A connection that goes quiet mid-request is answered and closed."""
    micro = Grelmicro(
        uses=[
            health,
            OpsServer(host="127.0.0.1", port=port, request_timeout=0.1),
        ]
    )
    async with micro:
        answer = await raw(
            port, b"GET /livez HTTP/1.1\r\nhost: probe\r\n", eof=False
        )

    assert status_of(answer) == HTTP_REQUEST_TIMEOUT


async def test_an_exporter_that_renders_nothing_serves_no_metrics(
    port: int, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the prometheus exporter has an exposition to serve."""
    micro = Grelmicro(
        uses=[
            Metrics(exporter=MetricsExporterType.NONE),
            OpsServer(host="127.0.0.1", port=port),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="grelmicro.http"):
        async with micro:
            assert micro.get("ops", "default").paths == ()
            answer = await raw(
                port, b"GET /metrics HTTP/1.1\r\nhost: probe\r\n\r\n"
            )

    assert status_of(answer) == HTTP_NOT_FOUND
    assert "only 'prometheus' renders an exposition" in caplog.text


async def test_a_failing_endpoint_answers_500(
    port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint that raises is logged and answered, not left hanging."""
    micro = Grelmicro(
        uses=[
            Metrics(exporter=MetricsExporterType.PROMETHEUS),
            OpsServer(host="127.0.0.1", port=port),
        ]
    )
    async with micro:
        assert micro.get("ops", "default").paths == ("/metrics",)
        # The component the route resolves per request stops being able to
        # render, which is every way an endpoint raises at request time.
        monkeypatch.setattr(
            micro.metrics,
            "_resolved",
            MetricsConfig(exporter=MetricsExporterType.NONE),
        )
        answer = await raw(
            port, b"GET /metrics HTTP/1.1\r\nhost: probe\r\n\r\n"
        )

    assert status_of(answer) == HTTP_INTERNAL_SERVER_ERROR


async def test_beyond_max_connections_a_caller_is_turned_away(
    port: int,
) -> None:
    """A flood is refused rather than allowed to exhaust the process."""
    health = HealthChecks(cache_ttl=0)
    health.add("slow", slow(0.5))
    micro = Grelmicro(
        uses=[
            health,
            OpsServer(host="127.0.0.1", port=port, max_connections=1),
        ]
    )
    async with micro:
        held = asyncio.create_task(
            raw(port, b"GET /readyz HTTP/1.1\r\nhost: probe\r\n\r\n")
        )
        await asyncio.sleep(0.1)
        turned_away = await raw(
            port, b"GET /livez HTTP/1.1\r\nhost: probe\r\n\r\n"
        )
        assert status_of(await held) == HTTP_200_OK

    assert status_of(turned_away) == HTTP_503_SERVICE_UNAVAILABLE


# --- How it stops --------------------------------------------------------


async def test_shutdown_lets_an_answer_finish(port: int) -> None:
    """A request in flight when the app closes still gets its answer."""
    health = HealthChecks(cache_ttl=0)
    health.add("slow", slow(0.2))
    micro = Grelmicro(uses=[health, OpsServer(host="127.0.0.1", port=port)])
    async with micro:
        held = asyncio.create_task(
            raw(port, b"GET /readyz HTTP/1.1\r\nhost: probe\r\n\r\n")
        )
        await asyncio.sleep(0.05)

    assert status_of(await held) == HTTP_200_OK


async def test_shutdown_cancels_what_does_not_finish(port: int) -> None:
    """With no grace, a request still running when the app closes is cut."""
    health = HealthChecks(cache_ttl=0)
    health.add("slow", slow(5.0))
    micro = Grelmicro(
        uses=[
            health,
            OpsServer(host="127.0.0.1", port=port, shutdown_timeout=0),
        ]
    )
    async with micro:
        held = asyncio.create_task(
            raw(port, b"GET /readyz HTTP/1.1\r\nhost: probe\r\n\r\n")
        )
        await asyncio.sleep(0.05)

    assert await held == b""


async def test_the_port_is_free_again_after_shutdown(
    health: HealthChecks, port: int
) -> None:
    """The listener is closed, so the same port binds again."""
    for _ in range(2):
        micro = Grelmicro(uses=[health, OpsServer(host="127.0.0.1", port=port)])
        async with micro:
            server = micro.get("ops", "default")
            assert server.paths
        assert server.paths == ()


# --- Configuration -------------------------------------------------------


def test_defaults_are_the_documented_ones() -> None:
    """The bare constructor binds every interface on 8080."""
    server = OpsServer()

    assert server.name == "default"
    assert server.config.host == ""
    assert server.config.port == 8080  # noqa: PLR2004
    assert server.config.show_details is False


def test_the_environment_tunes_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment moves the port without touching the code."""
    monkeypatch.setenv("GREL_OPS_PORT", "9100")

    assert OpsServer().config.port == 9100  # noqa: PLR2004


def test_a_named_server_reads_its_own_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second server on one app has a namespace of its own."""
    monkeypatch.setenv("GREL_OPS_ADMIN_PORT", "9200")

    assert OpsServer(name="admin").config.port == 9200  # noqa: PLR2004


def test_from_config_takes_the_config_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declarative door bypasses the environment."""
    monkeypatch.setenv("GREL_OPS_PORT", "9100")

    server = OpsServer.from_config(OpsServerConfig(port=9300), name="admin")

    assert server.config.port == 9300  # noqa: PLR2004
    assert server.name == "admin"


def test_a_port_outside_the_range_is_refused() -> None:
    """A port is a port, and the error says so at construction."""
    with pytest.raises(ValueError, match="less than or equal to 65535"):
        OpsServer(port=70000)


# --- The ASGI side of the server -----------------------------------------


async def test_it_drives_an_asgi_app_to_completion() -> None:
    """The server hands the app an empty body and joins what it sends back."""

    async def app(_scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        assert await receive() == {
            "type": "http.request",
            "body": b"",
            "more_body": False,
        }
        await send(
            {
                "type": "http.response.start",
                "status": HTTP_200_OK,
                "headers": [(b"content-length", b"5")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"one", "more_body": True}
        )
        await send({"type": "http.response.body", "body": b"two"})

    status, headers, body = await _call_app(app, {"type": "http"})

    assert status == HTTP_200_OK
    assert headers == [(b"content-length", b"5")]
    assert body == b"onetwo"


async def test_two_servers_run_on_two_ports(health: HealthChecks) -> None:
    """A second server under its own name listens beside the first."""
    first, second = free_port(), free_port()
    micro = Grelmicro(
        uses=[
            health,
            OpsServer(host="127.0.0.1", port=first),
            OpsServer(name="admin", host="127.0.0.1", port=second),
        ]
    )
    async with micro, httpx.AsyncClient() as client:
        for port in (first, second):
            response = await client.get(f"http://127.0.0.1:{port}/livez")
            assert response.status_code == HTTP_200_OK


@pytest.mark.usefixtures("serving")
async def test_two_lengths_that_disagree_are_refused(port: int) -> None:
    """A pair of `Content-Length` headers is how a request is smuggled."""
    answer = await raw(
        port,
        b"GET /livez HTTP/1.1\r\ncontent-length: 0\r\ncontent-length: 5\r\n\r\n",
    )

    assert status_of(answer) == HTTP_BAD_REQUEST


@pytest.mark.usefixtures("serving")
async def test_the_same_length_twice_is_read_once(port: int) -> None:
    """Repeated and agreeing is not malformed, so the request answers."""
    answer = await raw(
        port,
        b"GET /livez HTTP/1.1\r\ncontent-length: 2\r\ncontent-length: 2\r\n\r\nhi",
    )

    assert status_of(answer) == HTTP_200_OK


# --- While the app is still starting -------------------------------------


class _SlowToOpen:
    """A component that takes its time, the way a broker connection does."""

    kind = "slowdep"
    name = "default"

    def __init__(self, *, ready: asyncio.Event, release: asyncio.Event) -> None:
        self._ready = ready
        self._release = release

    async def __aenter__(self) -> Self:
        self._ready.set()
        await self._release.wait()
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_it_is_alive_but_not_ready_while_the_app_opens(
    port: int,
) -> None:
    """A pod is not Ready while the component after this one is connecting.

    `OpsServer` is registered first, so it binds its port while the rest of
    the app is still opening, and a `HealthChecks` after it has not
    registered its checks yet. Readiness must not answer for a check table
    that is still being built.
    """
    ready, release, opened, done = (asyncio.Event() for _ in range(4))
    micro = Grelmicro(
        uses=[
            OpsServer(host="127.0.0.1", port=port),
            _SlowToOpen(ready=ready, release=release),
            HealthChecks(),
        ]
    )

    async def run() -> None:
        async with micro:
            opened.set()
            await done.wait()

    running = asyncio.create_task(run())
    try:
        await ready.wait()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}"
        ) as client:
            assert (await client.get("/livez")).status_code == HTTP_200_OK
            assert (
                await client.get("/readyz")
            ).status_code == HTTP_503_SERVICE_UNAVAILABLE
            assert (
                await client.get("/healthz")
            ).status_code == HTTP_503_SERVICE_UNAVAILABLE

            release.set()
            await opened.wait()

            assert (await client.get("/readyz")).status_code == HTTP_200_OK
            assert (await client.get("/healthz")).status_code == HTTP_200_OK
    finally:
        release.set()
        done.set()
        await running


# --- Answering exactly once ----------------------------------------------


async def test_nothing_is_written_over_a_response_already_sent() -> None:
    """A deadline that lands mid-write closes rather than appending.

    Two status lines on one connection is worse than a truncated body: the
    client reads the second one as part of the first response.
    """
    server = OpsServer()
    writer = AsyncMock()
    answer = _Answer(writer, started=True, status=HTTP_REQUEST_TIMEOUT)

    await server._close(AsyncMock(), writer, answer)

    assert writer.write.call_args_list == []
    writer.close.assert_called_once()


# --- Refusals reach the caller -------------------------------------------


@pytest.mark.usefixtures("serving")
async def test_a_refused_body_still_reads_its_answer(port: int) -> None:
    """A caller mid-body gets the 413, not a reset connection.

    The refusal is written on a socket that still holds the body it
    refused, so the bytes are read and dropped before the close.
    """
    answer = await raw(
        port,
        b"GET /livez HTTP/1.1\r\ncontent-length: 99999\r\n\r\n" + b"x" * 9000,
        eof=False,
    )

    assert status_of(answer) == HTTP_CONTENT_TOO_LARGE


@pytest.mark.usefixtures("serving")
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(
            b"content-length: 0\r\ncontent-length: 00\r\n",
            HTTP_200_OK,
            id="same-length-written-twice",
        ),
        pytest.param(
            b"content-length: 1_0\r\n", HTTP_BAD_REQUEST, id="underscore"
        ),
        pytest.param(b"content-length: +2\r\n", HTTP_BAD_REQUEST, id="signed"),
    ],
)
async def test_content_length_is_read_as_a_number(
    port: int, headers: bytes, expected: int
) -> None:
    """Only digits are a length, and repeated ones have to agree."""
    answer = await raw(port, b"GET /livez HTTP/1.1\r\n" + headers + b"\r\n")

    assert status_of(answer) == expected
