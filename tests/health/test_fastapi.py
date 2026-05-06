"""Tests for FastAPI Health Check Router."""

import importlib
import sys
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request as _Request
from fastapi.params import Depends as _DependsParam
from fastapi.testclient import TestClient
from starlette.status import (
    HTTP_200_OK,
    HTTP_401_UNAUTHORIZED,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from grelmicro.errors import DependencyNotFoundError
from grelmicro.health._backends import health_registry
from grelmicro.health._registry import HealthRegistry
from grelmicro.health.fastapi import health_router

from .conftest import healthy, healthy_with_details, unhealthy

pytestmark = [pytest.mark.timeout(10)]


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None]:
    """Reset global health registry before and after each test."""
    health_registry.reset()
    yield
    health_registry.reset()


@pytest.fixture
def registry() -> HealthRegistry:
    """Health registry with caching disabled, registered as the default."""
    instance = HealthRegistry(cache_ttl=0)
    health_registry.register(instance, "default")
    return instance


@pytest.fixture
def app() -> FastAPI:
    """FastAPI app with default health router."""
    application = FastAPI()
    application.include_router(health_router())
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Test client for the FastAPI app."""
    return TestClient(app)


# ---------- /livez ----------


def test_livez_returns_empty_200(client: TestClient) -> None:
    """Livez returns 200 with an empty body."""
    response = client.get("/livez")

    assert response.status_code == HTTP_200_OK
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_livez_head_method(client: TestClient) -> None:
    """Livez accepts HEAD."""
    response = client.head("/livez")

    assert response.status_code == HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"


def test_livez_never_runs_checkers() -> None:
    """A failing registered checker does not affect /livez."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("db", unhealthy())
    app = FastAPI()
    app.include_router(health_router())
    client = TestClient(app)

    response = client.get("/livez")

    assert response.status_code == HTTP_200_OK
    assert response.content == b""


# ---------- /readyz ----------


@pytest.mark.usefixtures("registry")
def test_readyz_ok_when_no_checkers(client: TestClient) -> None:
    """Readyz returns 200 with empty body when no checkers."""
    response = client.get("/readyz")

    assert response.status_code == HTTP_200_OK
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_readyz_ok_with_healthy_critical(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Readyz returns 200 when critical checkers pass."""
    registry.add("db", healthy())

    response = client.get("/readyz")

    assert response.status_code == HTTP_200_OK
    assert response.content == b""


def test_readyz_503_on_critical_failure(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Readyz returns 503 when any critical check fails."""
    registry.add("db", unhealthy())

    response = client.get("/readyz")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert response.content == b""


def test_readyz_ignores_non_critical(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Readyz skips non-critical checkers entirely."""
    registry.add("db", healthy())
    registry.add("analytics", unhealthy(), critical=False)

    response = client.get("/readyz")

    assert response.status_code == HTTP_200_OK


def test_readyz_exclude_critical_checker(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Excluding the failing critical checker makes /readyz pass."""
    registry.add("db", unhealthy())
    registry.add("cache", healthy())

    response = client.get("/readyz?exclude=db")

    assert response.status_code == HTTP_200_OK


def test_readyz_exclude_multiple_comma_separated(
    registry: HealthRegistry, client: TestClient
) -> None:
    """The ?exclude param accepts a comma-separated list."""
    registry.add("db", unhealthy())
    registry.add("cache", unhealthy())

    response = client.get("/readyz?exclude=db,cache")

    assert response.status_code == HTTP_200_OK


def test_readyz_head_method(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Readyz accepts HEAD."""
    registry.add("db", unhealthy())

    response = client.head("/readyz")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE


# ---------- /healthz ----------


@pytest.mark.usefixtures("registry")
def test_healthz_empty_ok(client: TestClient) -> None:
    """Healthz with no checkers is ok with empty checks dict."""
    response = client.get("/healthz")

    assert response.status_code == HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"status": "ok", "checks": {}}


def test_healthz_includes_all_checkers(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Healthz includes critical and non-critical in the checks dict."""
    registry.add("db", healthy())
    registry.add("analytics", unhealthy(), critical=False)

    response = client.get("/healthz")

    assert response.status_code == HTTP_200_OK
    data = response.json()
    # non-critical failure does NOT flip the aggregate
    assert data["status"] == "ok"
    assert set(data["checks"]) == {"db", "analytics"}
    assert data["checks"]["db"]["status"] == "ok"
    assert data["checks"]["analytics"]["status"] == "error"
    assert data["checks"]["analytics"]["critical"] is False


def test_healthz_503_on_critical_failure(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Healthz returns 503 when any critical check fails."""
    registry.add("db", unhealthy())

    response = client.get("/healthz")

    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["status"] == "error"


def test_healthz_details_hidden_by_default(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Details are stripped from /healthz by default."""
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    response = client.get("/healthz")

    assert "details" not in response.json()["checks"]["redis"]


def test_healthz_details_true_always_shown() -> None:
    """show_details=True always includes details."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))
    app = FastAPI()
    app.include_router(health_router(show_details=True))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.json()["checks"]["redis"]["details"] == {"latency_ms": 1.5}


def test_healthz_details_dep_returns_false_strips() -> None:
    """A dep returning False strips details, endpoint returns 200."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    def allow() -> bool:
        return False

    app = FastAPI()
    app.include_router(health_router(show_details=Depends(allow)))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == HTTP_200_OK
    assert "details" not in response.json()["checks"]["redis"]


def test_healthz_details_dep_returns_true_shows() -> None:
    """A dep returning True includes details."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    def allow() -> bool:
        return True

    app = FastAPI()
    app.include_router(health_router(show_details=Depends(allow)))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.json()["checks"]["redis"]["details"] == {"latency_ms": 1.5}


def test_healthz_details_async_dep_shows() -> None:
    """An async dep is awaited by FastAPI's DI."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    async def allow_async() -> bool:
        return True

    app = FastAPI()
    app.include_router(health_router(show_details=Depends(allow_async)))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.json()["checks"]["redis"]["details"] == {"latency_ms": 1.5}


def test_healthz_details_dep_with_request() -> None:
    """A Request-annotated dep receives the request via FastAPI DI."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    def allow_if_admin(request: _Request) -> bool:
        return request.headers.get("x-admin") == "yes"

    app = FastAPI()
    app.include_router(health_router(show_details=Depends(allow_if_admin)))
    client = TestClient(app)

    assert "details" not in client.get("/healthz").json()["checks"]["redis"]
    allowed = client.get("/healthz", headers={"x-admin": "yes"})
    assert allowed.json()["checks"]["redis"]["details"] == {"latency_ms": 1.5}


def test_healthz_details_dep_with_sub_dependency() -> None:
    """FastAPI sub-dependencies resolve through ``Depends`` chains."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    def current_role(request: _Request) -> str:
        return request.headers.get("x-role", "guest")

    def is_admin(role: str = Depends(current_role)) -> bool:
        return role == "admin"

    app = FastAPI()
    app.include_router(health_router(show_details=Depends(is_admin)))
    client = TestClient(app)

    assert "details" not in client.get("/healthz").json()["checks"]["redis"]
    allowed = client.get("/healthz", headers={"x-role": "admin"})
    assert allowed.json()["checks"]["redis"]["details"] == {"latency_ms": 1.5}


def test_healthz_details_dep_http_exception_blocks_endpoint() -> None:
    """Raising HTTPException in the dep blocks the endpoint (documented)."""
    registry = HealthRegistry(cache_ttl=0)
    health_registry.register(registry, "default")
    registry.add("redis", healthy_with_details({"latency_ms": 1.5}))

    def deny() -> bool:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)

    app = FastAPI()
    app.include_router(health_router(show_details=Depends(deny)))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == HTTP_401_UNAUTHORIZED


def test_healthz_details_dep_none_rejected() -> None:
    """``Depends(None)`` is rejected at router build time."""
    with pytest.raises(TypeError, match="Depends\\(None\\)"):
        health_router(show_details=_DependsParam(dependency=None))


def test_healthz_details_invalid_type_rejected() -> None:
    """An invalid ``show_details`` value is rejected at router build time."""
    with pytest.raises(TypeError, match="show_details"):
        health_router(show_details="yes")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_healthz_exclude_checker(
    registry: HealthRegistry, client: TestClient
) -> None:
    """?exclude removes the named checker from the response."""
    registry.add("db", healthy())
    registry.add("redis", unhealthy())

    response = client.get("/healthz?exclude=redis")

    data = response.json()
    assert set(data["checks"]) == {"db"}
    assert data["status"] == "ok"
    assert response.status_code == HTTP_200_OK


def test_healthz_dependencies_block_endpoint() -> None:
    """healthz_dependencies block the entire endpoint on failure."""
    HealthRegistry(cache_ttl=0)

    def deny() -> None:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)

    app = FastAPI()
    app.include_router(health_router(healthz_dependencies=[Depends(deny)]))
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == HTTP_401_UNAUTHORIZED


def test_healthz_head_method(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Healthz accepts HEAD."""
    registry.add("db", healthy())

    response = client.head("/healthz")

    assert response.status_code == HTTP_200_OK
    assert response.headers["cache-control"] == "no-store"


# ---------- OpenAPI + misc ----------


@pytest.mark.usefixtures("registry")
def test_openapi_schema(client: TestClient) -> None:
    """All three endpoints appear in the OpenAPI schema."""
    schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    assert "/livez" in paths
    assert "/readyz" in paths
    assert "/healthz" in paths
    healthz_responses = paths["/healthz"]["get"]["responses"]
    assert "200" in healthz_responses
    assert "503" in healthz_responses


def test_health_router_raises_without_fastapi() -> None:
    """health_router raises DependencyNotFoundError without FastAPI."""
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
def test_router_prefix() -> None:
    """The prefix kwarg mounts endpoints under a custom path."""
    app = FastAPI()
    app.include_router(health_router(prefix="/api/v1"))
    client = TestClient(app)

    assert client.get("/api/v1/livez").status_code == HTTP_200_OK
    assert client.get("/api/v1/readyz").status_code == HTTP_200_OK
    assert client.get("/api/v1/healthz").status_code == HTTP_200_OK


def test_registry_unhealthy_produces_503_on_both_endpoints(
    registry: HealthRegistry, client: TestClient
) -> None:
    """Critical failure flips both /readyz and /healthz to 503."""
    registry.add("db", unhealthy())

    assert client.get("/readyz").status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert client.get("/healthz").status_code == HTTP_503_SERVICE_UNAVAILABLE
