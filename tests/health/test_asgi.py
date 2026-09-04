"""Tests for the pure-ASGI health endpoints."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from litestar import Litestar
from litestar.handlers import asgi
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks, health_asgi
from tests.health.conftest import healthy, healthy_with_details, unhealthy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405


def client_for(app: Any) -> httpx.AsyncClient:  # noqa: ANN401
    """Return a client that drives any ASGI app over the ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://probe",
    )


@pytest.fixture
def health() -> HealthChecks:
    """Return a health component with caching off."""
    return HealthChecks(cache_ttl=0)


@pytest.fixture
async def client(health: HealthChecks) -> AsyncIterator[httpx.AsyncClient]:
    """Return a client for the app serving `health`."""
    async with client_for(health_asgi(health)) as client:
        yield client


async def test_livez_answers_without_running_checks(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """A failing check leaves /livez alone."""
    health.add("db", unhealthy())

    response = await client.get("/livez")

    assert response.status_code == HTTP_200_OK
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == "0"
    assert "content-type" not in response.headers


async def test_readyz_answers_the_code_alone(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """/readyz reports readiness with a status code and an empty body."""
    health.add("db", healthy())

    assert (await client.get("/readyz")).status_code == HTTP_200_OK

    health.add("cache", unhealthy())

    response = await client.get("/readyz")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert response.content == b""


async def test_healthz_reports_every_check(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """/healthz answers the aggregate report as JSON."""
    health.add("db", healthy())
    health.add("analytics", unhealthy(), critical=False)

    response = await client.get("/healthz")

    assert response.status_code == HTTP_200_OK
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "status": "ok",
        "checks": {
            "db": {"status": "ok", "critical": True},
            "analytics": {
                "status": "error",
                "critical": False,
                "error": "ConnectionError: Connection refused",
            },
        },
    }


async def test_healthz_hides_details_by_default(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """Verbose details are stripped unless the app was asked to show them."""
    health.add("redis", healthy_with_details({"latency_ms": 1.5}))

    response = await client.get("/healthz")

    assert "details" not in response.json()["checks"]["redis"]


async def test_healthz_shows_details_when_asked(
    health: HealthChecks,
) -> None:
    """show_details=True includes the details a check returned."""
    health.add("redis", healthy_with_details({"latency_ms": 1.5}))

    async with client_for(health_asgi(health, show_details=True)) as client:
        response = await client.get("/healthz")

    assert response.json()["checks"]["redis"]["details"] == {"latency_ms": 1.5}


@pytest.mark.parametrize("path", ["/readyz", "/healthz"])
async def test_exclude_skips_the_named_checks(
    health: HealthChecks, client: httpx.AsyncClient, path: str
) -> None:
    """?exclude= drops a check from the run, so a failing one is muted."""
    health.add("db", healthy())
    health.add("flaky", unhealthy())

    assert (await client.get(path)).status_code == HTTP_503_SERVICE_UNAVAILABLE

    response = await client.get(f"{path}?exclude=flaky")

    assert response.status_code == HTTP_200_OK


async def test_exclude_reads_the_first_value(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """A repeated parameter is read once, from the first occurrence."""
    health.add("flaky", unhealthy())

    response = await client.get("/readyz?exclude=flaky&exclude=other")

    assert response.status_code == HTTP_200_OK


async def test_exclude_left_blank_excludes_nothing(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """An empty value is not a check name."""
    health.add("db", healthy())

    response = await client.get("/healthz?exclude=")

    assert list(response.json()["checks"]) == ["db"]


async def test_query_string_without_exclude_is_ignored(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """Another parameter changes nothing."""
    health.add("db", healthy())

    response = await client.get("/healthz?verbose=1")

    assert list(response.json()["checks"]) == ["db"]


async def test_head_carries_the_length_without_the_body(
    health: HealthChecks, client: httpx.AsyncClient
) -> None:
    """HEAD answers what GET would, minus the bytes."""
    health.add("db", healthy())

    head = await client.head("/healthz")
    get = await client.get("/healthz")

    assert head.status_code == get.status_code
    assert head.headers["content-length"] == str(len(get.content))


async def test_unknown_path_is_not_found(client: httpx.AsyncClient) -> None:
    """The app serves its three paths and nothing else."""
    response = await client.get("/health")

    assert response.status_code == HTTP_NOT_FOUND
    assert response.content == b""


async def test_write_method_is_refused(client: httpx.AsyncClient) -> None:
    """A probe is a read, so anything else is answered 405."""
    response = await client.post("/livez")

    assert response.status_code == HTTP_METHOD_NOT_ALLOWED
    assert response.headers["allow"] == "GET, HEAD"


async def test_prefix_moves_the_three_paths(health: HealthChecks) -> None:
    """prefix= mounts the endpoints under a path of your own."""
    health.add("db", healthy())

    async with client_for(health_asgi(health, prefix="/internal")) as client:
        assert (await client.get("/internal/livez")).status_code == HTTP_200_OK
        assert (await client.get("/livez")).status_code == HTTP_NOT_FOUND


async def test_mounted_under_another_app(health: HealthChecks) -> None:
    """A mount adds a prefix the app answers under, with no prefix= of its own."""
    health.add("db", healthy())
    app = Starlette(routes=[Mount("/ops", app=health_asgi(health))])

    async with client_for(app) as client:
        assert (await client.get("/ops/livez")).status_code == HTTP_200_OK
        assert (await client.get("/ops/healthz")).status_code == HTTP_200_OK
        assert (await client.get("/ops/nope")).status_code == HTTP_NOT_FOUND


async def test_mounted_on_litestar(health: HealthChecks) -> None:
    """Litestar hands a mount the leftover path, and it still matches.

    Mounted at the root it arrives as `livez/`, with no leading slash and
    a trailing one, which is neither what Starlette hands nor what the
    route is written as.
    """
    health.add("db", healthy())
    app = Litestar(
        route_handlers=[
            asgi("/", is_mount=True, copy_scope=True)(health_asgi(health))
        ]
    )

    async with client_for(app) as client:
        assert (await client.get("/livez")).status_code == HTTP_200_OK
        assert (await client.get("/healthz")).status_code == HTTP_200_OK
        assert (await client.get("/nope")).status_code == HTTP_NOT_FOUND


async def test_a_trailing_slash_reaches_the_same_probe(
    client: httpx.AsyncClient,
) -> None:
    """`/livez/` is the same probe as `/livez`."""
    assert (await client.get("/livez/")).status_code == HTTP_200_OK


async def test_component_resolves_from_the_active_app() -> None:
    """With no component passed, the app answers from the running one."""
    health = HealthChecks(cache_ttl=0)
    health.add("db", healthy())
    micro = Grelmicro(uses=[health])

    async with micro, client_for(health_asgi()) as client:
        response = await client.get("/healthz")

    assert response.json()["checks"]["db"] == {
        "status": "ok",
        "critical": True,
    }


async def test_lifespan_protocol_is_answered(health: HealthChecks) -> None:
    """The app can be served on its own, so it completes the lifespan."""
    app = health_asgi(health)
    sent: list[dict[str, Any]] = []
    received = iter(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )

    async def receive() -> Any:  # noqa: ANN401
        return next(received)

    async def send(message: Any) -> None:  # noqa: ANN401
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert [message["type"] for message in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


async def test_a_websocket_is_closed_rather_than_dropped(
    health: HealthChecks,
) -> None:
    """Mounted at the root, a websocket reaches the app. It is not ours.

    Returning without a word leaves the server reporting a handshake that
    never came, so the connection is closed cleanly instead.
    """
    app = health_asgi(health)
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:  # noqa: ANN401
        return {"type": "websocket.connect"}

    async def send(message: Any) -> None:  # noqa: ANN401
        sent.append(message)

    await app({"type": "websocket", "path": "/livez"}, receive, send)

    assert sent == [{"type": "websocket.close", "code": 1001}]


async def test_other_scopes_are_left_alone(health: HealthChecks) -> None:
    """A scope that is neither HTTP, lifespan, nor a websocket is not ours."""
    app = health_asgi(health)
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:  # noqa: ANN401
        raise AssertionError

    async def send(message: Any) -> None:  # noqa: ANN401
        sent.append(message)

    await app({"type": "custom", "path": "/livez"}, receive, send)

    assert sent == []


async def test_served_directly_by_uvicorn(health: HealthChecks) -> None:
    """The app stands on its own under a real ASGI server.

    It answers the lifespan protocol, so uvicorn starts and stops it
    without complaining that the app supports none.
    """
    import uvicorn  # noqa: PLC0415

    health.add("db", healthy())
    # Bound here rather than in uvicorn, so the port is listening before
    # the server task starts and the client never races the startup.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(health_asgi(health), log_level="warning", lifespan="on")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}"
        ) as client:
            assert (await client.get("/livez")).status_code == HTTP_200_OK
            assert (await client.get("/healthz")).json()["status"] == "ok"
            assert (await client.get("/nope")).status_code == HTTP_NOT_FOUND
    finally:
        server.should_exit = True
        await serving
