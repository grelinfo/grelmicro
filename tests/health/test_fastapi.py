"""Tests for FastAPI Health Check Router."""

import importlib
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.status import (
    HTTP_200_OK,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from grelmicro.errors import DependencyNotFoundError
from grelmicro.health._models import HealthStatus
from grelmicro.health._registry import HealthRegistry
from grelmicro.health.fastapi import health_router

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


# --- Test helpers ---


class HealthyChecker:
    """A checker that always returns HEALTHY."""

    def __init__(self, name: str = "db") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> HealthStatus:
        """Return HEALTHY."""
        return HealthStatus.HEALTHY


class UnhealthyChecker:
    """A checker that always raises."""

    def __init__(self, name: str = "redis") -> None:
        """Initialize the checker."""
        self._name = name

    @property
    def name(self) -> str:
        """Return the checker name."""
        return self._name

    async def check(self) -> HealthStatus:
        """Raise ConnectionError."""
        msg = "Connection refused"
        raise ConnectionError(msg)


# --- Fixtures ---


@pytest.fixture
def registry() -> HealthRegistry:
    """Empty health registry."""
    return HealthRegistry()


@pytest.fixture
def app(registry: HealthRegistry) -> FastAPI:
    """FastAPI app with health router."""
    application = FastAPI()
    application.include_router(health_router(registry))
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Test client for the FastAPI app."""
    return TestClient(app)


# --- Tests ---


def test_liveness_always_healthy(client: TestClient) -> None:
    """Test that liveness endpoint always returns 200."""
    response = client.get("/health/live")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"status": "healthy"}


def test_readiness_healthy_with_no_checkers(client: TestClient) -> None:
    """Test that readiness returns 200 when no checkers are registered."""
    response = client.get("/health/ready")

    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["status"] == "healthy"
    assert data["components"] == []


def test_readiness_healthy_with_healthy_checker(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that readiness returns 200 when all checkers are healthy."""
    registry.add(HealthyChecker("db"))

    response = client.get("/health/ready")

    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["status"] == "healthy"
    assert len(data["components"]) == 1
    assert data["components"][0]["name"] == "db"
    assert data["components"][0]["status"] == "healthy"


def test_readiness_degraded_with_unhealthy_checker(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that readiness returns 503 when a checker is unhealthy."""
    registry.add(UnhealthyChecker("redis"))

    response = client.get("/health/ready")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "degraded"
    assert len(data["components"]) == 1
    assert data["components"][0]["status"] == "unhealthy"
    assert data["components"][0]["detail"] == "Connection refused"


def test_readiness_degraded_with_mixed_checkers(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that readiness returns 503 when some checkers are unhealthy."""
    registry.add(HealthyChecker("db"))
    registry.add(UnhealthyChecker("redis"))

    response = client.get("/health/ready")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "degraded"
    assert len(data["components"]) == len(["db", "redis"])


def test_openapi_schema_includes_response_models(
    client: TestClient,
) -> None:
    """Test that OpenAPI schema contains proper response models."""
    response = client.get("/openapi.json")
    schema = response.json()

    ready_path = schema["paths"]["/health/ready"]
    ready_responses = ready_path["get"]["responses"]
    assert "200" in ready_responses
    assert "503" in ready_responses

    live_path = schema["paths"]["/health/live"]
    live_responses = live_path["get"]["responses"]
    assert "200" in live_responses


def test_health_router_raises_without_fastapi() -> None:
    """Test that health_router raises DependencyNotFoundError without FastAPI."""
    with patch.dict(sys.modules, {"fastapi": None, "fastapi.responses": None}):
        if "grelmicro.health.fastapi" in sys.modules:
            del sys.modules["grelmicro.health.fastapi"]
        module = importlib.import_module("grelmicro.health.fastapi")

        registry = HealthRegistry()
        with pytest.raises(DependencyNotFoundError):
            module.health_router(registry)

    # Restore
    if "grelmicro.health.fastapi" in sys.modules:
        del sys.modules["grelmicro.health.fastapi"]
    importlib.import_module("grelmicro.health.fastapi")


def test_router_with_prefix(registry: HealthRegistry) -> None:
    """Test that the router respects the prefix parameter."""
    app = FastAPI()
    app.include_router(health_router(registry, prefix="/api"))
    client = TestClient(app)

    response = client.get("/api/health/live")
    assert response.status_code == HTTP_200_OK

    response = client.get("/api/health/ready")
    assert response.status_code == HTTP_200_OK
