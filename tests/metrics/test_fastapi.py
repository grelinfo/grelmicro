"""Tests for the FastAPI metrics router."""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.status import HTTP_200_OK, HTTP_401_UNAUTHORIZED

from grelmicro import Grelmicro
from grelmicro.errors import DependencyNotFoundError
from grelmicro.metrics import Metrics, MetricsExporterType, metrics_router
from grelmicro.metrics.errors import MetricsError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def prometheus_app() -> AsyncIterator[FastAPI]:
    """Enter a Grelmicro app with a Prometheus Metrics component."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with micro:
        # A counter so the exposition is non-empty.
        micro.metrics.counter("orders.placed", unit="1").add(1)
        application = FastAPI()
        application.include_router(metrics_router())
        # Resolution happens at request time via Grelmicro.current(), which
        # the running app sets for this task.
        yield application


async def test_metrics_endpoint_renders_prometheus(
    prometheus_app: FastAPI,
) -> None:
    """GET /metrics returns Prometheus exposition with the right content type."""
    client = TestClient(prometheus_app)
    response = client.get("/metrics")
    assert response.status_code == HTTP_200_OK
    assert response.headers["content-type"] == (
        "text/plain; version=0.0.4; charset=utf-8"
    )
    assert "orders_placed" in response.text


async def test_metrics_endpoint_explicit_component() -> None:
    """Passing `component=` bypasses app resolution."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with micro:
        application = FastAPI()
        application.include_router(metrics_router(micro.metrics))
        client = TestClient(application)
        assert client.get("/metrics").status_code == HTTP_200_OK


async def test_metrics_endpoint_custom_path_and_prefix() -> None:
    """`prefix` and `path` mount the endpoint at a custom location."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with micro:
        application = FastAPI()
        application.include_router(
            metrics_router(micro.metrics, prefix="/internal", path="/prom")
        )
        client = TestClient(application)
        assert client.get("/internal/prom").status_code == HTTP_200_OK


async def test_metrics_endpoint_requires_prometheus_exporter() -> None:
    """A non-Prometheus exporter raises `MetricsError` on request."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    async with micro:
        application = FastAPI()
        application.include_router(metrics_router(micro.metrics))
        client = TestClient(application, raise_server_exceptions=True)
        with pytest.raises(MetricsError, match="prometheus exporter"):
            client.get("/metrics")


async def test_metrics_endpoint_dependency_gate() -> None:
    """A failing dependency blocks the endpoint."""

    def deny() -> None:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)

    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with micro:
        application = FastAPI()
        application.include_router(
            metrics_router(micro.metrics, dependencies=[Depends(deny)])
        )
        client = TestClient(application)
        assert client.get("/metrics").status_code == HTTP_401_UNAUTHORIZED


def test_metrics_router_raises_without_fastapi() -> None:
    """metrics_router raises DependencyNotFoundError without FastAPI."""
    with patch.dict(sys.modules, {"fastapi": None, "fastapi.responses": None}):
        if "grelmicro.metrics.fastapi" in sys.modules:
            del sys.modules["grelmicro.metrics.fastapi"]
        module = importlib.import_module("grelmicro.metrics.fastapi")

        with pytest.raises(DependencyNotFoundError):
            module.metrics_router()

    if "grelmicro.metrics.fastapi" in sys.modules:
        del sys.modules["grelmicro.metrics.fastapi"]
    importlib.import_module("grelmicro.metrics.fastapi")  # restore
