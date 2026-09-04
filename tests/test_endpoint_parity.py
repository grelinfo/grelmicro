"""The FastAPI door and the framework-free door answer the same.

`health_router()` and `health_asgi()` serve the same three endpoints, and
`metrics_router()` and `metrics_asgi()` the same one. They render through
one set of functions, and this holds them to it: the status line, the
headers that carry meaning, and the body byte for byte.

A client must not be able to tell which door answered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks, health_asgi
from grelmicro.integrations.fastapi import health_router
from grelmicro.metrics import (
    Metrics,
    MetricsExporterType,
    metrics_asgi,
    metrics_router,
)
from tests.health.conftest import (
    healthy,
    healthy_with_details,
    unhealthy,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    Answer = tuple[int, dict[str, str], bytes]

_COMPARED_HEADERS = ("cache-control", "content-type", "content-length")
"""Headers that carry meaning. `date` and `server` are the server's own."""


def _answer(response: httpx.Response) -> Answer:
    """Reduce a response to what a client can tell two doors apart by."""
    return (
        response.status_code,
        {
            name: value
            for name, value in response.headers.items()
            if name in _COMPARED_HEADERS
        },
        response.content,
    )


def client_for(app: Any) -> httpx.AsyncClient:  # noqa: ANN401
    """Return a client that drives any ASGI app over the ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://probe",
    )


@pytest.fixture
def health() -> HealthChecks:
    """Return a health component carrying one of each kind of check."""
    checks = HealthChecks(cache_ttl=0)
    checks.add("store", healthy())
    checks.add("redis", healthy_with_details({"latency_ms": 1.5}))
    checks.add("analytics", unhealthy(), critical=False)
    return checks


@pytest.fixture
async def doors(
    health: HealthChecks,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """Return a client for the FastAPI router and one for the ASGI app."""
    app = FastAPI()
    app.include_router(health_router(health, show_details=True))
    async with (
        client_for(app) as router,
        client_for(health_asgi(health, show_details=True)) as asgi,
    ):
        yield router, asgi


@pytest.mark.parametrize(
    "path",
    [
        "/livez",
        "/readyz",
        "/healthz",
        "/readyz?exclude=analytics",
        "/healthz?exclude=analytics,store",
        # Repeated: a framework hands a scalar parameter the last value,
        # and a door that read the first would answer for other checks.
        "/readyz?exclude=store&exclude=analytics",
        "/healthz?exclude=store&exclude=analytics",
    ],
)
async def test_health_doors_answer_the_same(
    doors: tuple[httpx.AsyncClient, httpx.AsyncClient], path: str
) -> None:
    """Every health path answers identically through both doors."""
    router, asgi = doors

    assert _answer(await router.get(path)) == _answer(await asgi.get(path))


async def test_health_doors_agree_when_a_critical_check_fails(
    health: HealthChecks, doors: tuple[httpx.AsyncClient, httpx.AsyncClient]
) -> None:
    """The `503` and its body are the same through both doors."""
    health.add("database", unhealthy())
    router, asgi = doors

    assert _answer(await router.get("/readyz")) == _answer(
        await asgi.get("/readyz")
    )
    assert _answer(await router.get("/healthz")) == _answer(
        await asgi.get("/healthz")
    )


async def test_health_doors_strip_details_the_same_way(
    health: HealthChecks,
) -> None:
    """With details off, both doors leave the same field out."""
    app = FastAPI()
    app.include_router(health_router(health))
    async with (
        client_for(app) as router,
        client_for(health_asgi(health)) as asgi,
    ):
        assert _answer(await router.get("/healthz")) == _answer(
            await asgi.get("/healthz")
        )


async def test_metrics_doors_answer_the_same() -> None:
    """The exposition, its content type, and its length all match."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with micro:
        micro.metrics.counter("orders.placed", unit="1").add(1)
        app = FastAPI()
        app.include_router(metrics_router(micro.metrics))
        async with (
            client_for(app) as router,
            client_for(metrics_asgi(micro.metrics)) as asgi,
        ):
            through_router = _answer(await router.get("/metrics"))
            through_asgi = _answer(await asgi.get("/metrics"))

    assert through_router == through_asgi
