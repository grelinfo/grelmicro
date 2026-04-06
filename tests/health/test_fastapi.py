"""Tests for FastAPI Health Check Router."""

import importlib
import sys
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.status import (
    HTTP_200_OK,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from grelmicro.errors import DependencyNotFoundError
from grelmicro.health._registry import HealthRegistry
from grelmicro.health._state import reset_health_registry
from grelmicro.health.fastapi import health_router

from .conftest import (
    HealthyChecker,
    HealthyCheckerWithDetails,
    UnhealthyChecker,
)

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None]:
    """Reset global health registry before and after each test."""
    reset_health_registry()
    yield
    reset_health_registry()


@pytest.fixture
def registry() -> HealthRegistry:
    """Auto-registered health registry."""
    return HealthRegistry()


@pytest.fixture
def app() -> FastAPI:
    """FastAPI app with health router (details hidden by default)."""
    application = FastAPI()
    application.include_router(health_router())
    return application


@pytest.fixture
def app_with_details() -> FastAPI:
    """FastAPI app with health router (details shown by default)."""
    application = FastAPI()
    application.include_router(health_router(show_details=True))
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def client_with_details(app_with_details: FastAPI) -> TestClient:
    """Test client for the FastAPI app with details enabled."""
    return TestClient(app_with_details)


def test_liveness_always_healthy(client: TestClient) -> None:
    """Test that liveness endpoint always returns 200."""
    response = client.get("/health/live")

    assert response.status_code == HTTP_200_OK
    assert response.json() == {"status": "healthy"}


@pytest.mark.usefixtures("registry")
def test_readiness_healthy_with_no_checkers(
    client: TestClient,
) -> None:
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
    assert data["components"][0]["error"] == "Health check failed"


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
    assert [c["name"] for c in data["components"]] == ["db", "redis"]


def test_details_hidden_by_default(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that details are stripped from the response by default."""
    registry.add(HealthyCheckerWithDetails("redis", {"latency_ms": 1.5}))

    response = client.get("/health/ready")

    assert response.status_code == HTTP_200_OK
    component = response.json()["components"][0]
    assert "details" not in component


def test_details_shown_with_query_param(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that details are included when ?details=true is passed."""
    registry.add(HealthyCheckerWithDetails("redis", {"latency_ms": 1.5}))

    response = client.get("/health/ready?details=true")

    assert response.status_code == HTTP_200_OK
    component = response.json()["components"][0]
    assert component["details"] == {"latency_ms": 1.5}


def test_details_shown_by_default_when_configured(
    registry: HealthRegistry,
    client_with_details: TestClient,
) -> None:
    """Test that details are shown when show_details=True is configured."""
    registry.add(HealthyCheckerWithDetails("redis", {"latency_ms": 1.5}))

    response = client_with_details.get("/health/ready")

    assert response.status_code == HTTP_200_OK
    component = response.json()["components"][0]
    assert component["details"] == {"latency_ms": 1.5}


def test_details_hidden_with_query_param_override(
    registry: HealthRegistry,
    client_with_details: TestClient,
) -> None:
    """Test that ?details=false overrides show_details=True."""
    registry.add(HealthyCheckerWithDetails("redis", {"latency_ms": 1.5}))

    response = client_with_details.get("/health/ready?details=false")

    assert response.status_code == HTTP_200_OK
    component = response.json()["components"][0]
    assert "details" not in component


def test_non_critical_failure_returns_200(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that non-critical checker failure still returns 200."""
    registry.add(HealthyChecker("db"))
    registry.add(UnhealthyChecker("external-api"), critical=False)

    response = client.get("/health/ready")

    assert response.status_code == HTTP_200_OK
    data = response.json()
    assert data["status"] == "healthy"
    components = {c["name"]: c for c in data["components"]}
    assert components["db"]["status"] == "healthy"
    assert components["external-api"]["status"] == "unhealthy"
    assert components["external-api"]["critical"] is False


def test_critical_failure_returns_503(
    registry: HealthRegistry,
    client: TestClient,
) -> None:
    """Test that critical checker failure returns 503."""
    registry.add(UnhealthyChecker("db"), critical=True)
    registry.add(HealthyChecker("external-api"), critical=False)

    response = client.get("/health/ready")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    data = response.json()
    assert data["status"] == "degraded"


@pytest.mark.usefixtures("registry")
def test_openapi_schema_includes_response_models(
    client: TestClient,
) -> None:
    """Test that OpenAPI schema contains proper response models."""
    response = client.get("/openapi.json")

    schema = response.json()
    ready_responses = schema["paths"]["/health/ready"]["get"]["responses"]
    live_responses = schema["paths"]["/health/live"]["get"]["responses"]
    assert "200" in ready_responses
    assert "503" in ready_responses
    assert "200" in live_responses


def test_health_router_raises_without_fastapi() -> None:
    """Test that health_router raises DependencyNotFoundError without FastAPI."""
    with patch.dict(sys.modules, {"fastapi": None, "fastapi.responses": None}):
        if "grelmicro.health.fastapi" in sys.modules:
            del sys.modules["grelmicro.health.fastapi"]
        module = importlib.import_module("grelmicro.health.fastapi")

        with pytest.raises(DependencyNotFoundError):
            module.health_router()

    if "grelmicro.health.fastapi" in sys.modules:
        del sys.modules["grelmicro.health.fastapi"]
    importlib.import_module("grelmicro.health.fastapi")  # restore


@pytest.mark.usefixtures("registry")
def test_router_with_prefix_liveness() -> None:
    """Test that the liveness endpoint respects the prefix parameter."""
    app = FastAPI()
    app.include_router(health_router(prefix="/api"))
    client = TestClient(app)

    response = client.get("/api/health/live")

    assert response.status_code == HTTP_200_OK


@pytest.mark.usefixtures("registry")
def test_router_with_prefix_readiness() -> None:
    """Test that the readiness endpoint respects the prefix parameter."""
    app = FastAPI()
    app.include_router(health_router(prefix="/api"))
    client = TestClient(app)

    response = client.get("/api/health/ready")

    assert response.status_code == HTTP_200_OK
