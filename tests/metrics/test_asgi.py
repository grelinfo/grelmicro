"""Tests for the pure-ASGI metrics endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.status import HTTP_200_OK

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsExporterType, metrics_asgi
from grelmicro.metrics.errors import MetricsError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
PROMETHEUS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def client_for(app: Any) -> httpx.AsyncClient:  # noqa: ANN401
    """Return a client that drives any ASGI app over the ASGI transport."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://probe",
    )


@pytest.fixture
async def micro() -> AsyncIterator[Grelmicro]:
    """Enter an app with a Prometheus Metrics component."""
    app = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with app:
        app.metrics.counter("orders.placed", unit="1").add(1)
        yield app


async def test_metrics_renders_the_exposition(micro: Grelmicro) -> None:
    """GET /metrics answers the exposition with the Prometheus content type."""
    async with client_for(metrics_asgi(micro.metrics)) as client:
        response = await client.get("/metrics")

    assert response.status_code == HTTP_200_OK
    assert response.headers["content-type"] == PROMETHEUS_MEDIA_TYPE
    assert response.headers["cache-control"] == "no-store"
    assert "orders_placed" in response.text


@pytest.mark.usefixtures("micro")
async def test_component_resolves_from_the_active_app() -> None:
    """With no component passed, the endpoint answers from the running app."""
    async with client_for(metrics_asgi()) as client:
        response = await client.get("/metrics")

    assert response.status_code == HTTP_200_OK
    assert "orders_placed" in response.text


async def test_path_and_prefix_move_the_endpoint(micro: Grelmicro) -> None:
    """prefix= and path= mount the endpoint where you want it."""
    app = metrics_asgi(micro.metrics, prefix="/internal", path="/prom")

    async with client_for(app) as client:
        assert (await client.get("/internal/prom")).status_code == HTTP_200_OK
        assert (await client.get("/metrics")).status_code == HTTP_NOT_FOUND


async def test_mounted_under_another_app(micro: Grelmicro) -> None:
    """A mount adds the prefix, and the endpoint answers under it."""
    app = Starlette(routes=[Mount("/ops", app=metrics_asgi(micro.metrics))])

    async with client_for(app) as client:
        assert (await client.get("/ops/metrics")).status_code == HTTP_200_OK


async def test_head_carries_the_length_without_the_body(
    micro: Grelmicro,
) -> None:
    """HEAD answers what GET would, minus the bytes."""
    async with client_for(metrics_asgi(micro.metrics)) as client:
        head = await client.head("/metrics")
        get = await client.get("/metrics")

    assert head.headers["content-length"] == str(len(get.content))


async def test_write_method_is_refused(micro: Grelmicro) -> None:
    """A scrape is a read, so anything else is answered 405."""
    async with client_for(metrics_asgi(micro.metrics)) as client:
        response = await client.post("/metrics")

    assert response.status_code == HTTP_METHOD_NOT_ALLOWED
    assert response.headers["allow"] == "GET, HEAD"


async def test_another_exporter_is_refused() -> None:
    """Only the prometheus exporter builds a registry to render."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    async with micro, client_for(metrics_asgi(micro.metrics)) as client:
        with pytest.raises(MetricsError, match="prometheus exporter"):
            await client.get("/metrics")
