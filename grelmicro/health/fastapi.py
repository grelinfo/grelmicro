"""FastAPI Health Check Router."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.health._models import HealthStatus, OverallStatus

if TYPE_CHECKING:
    from fastapi import APIRouter

    from grelmicro.health._registry import HealthRegistry


class ComponentHealthResponse(BaseModel):
    """Health status of a single component."""

    name: str
    status: HealthStatus
    detail: str | None


class LivenessResponse(BaseModel):
    """Liveness probe response."""

    status: Literal["healthy"] = "healthy"


class ReadinessResponse(BaseModel):
    """Readiness probe response."""

    status: OverallStatus
    components: list[ComponentHealthResponse]


def health_router(
    registry: Annotated[
        HealthRegistry,
        Doc("The health registry to use for readiness checks."),
    ],
    *,
    prefix: Annotated[
        str,
        Doc("URL prefix for health endpoints (e.g. '/api')."),
    ] = "",
) -> APIRouter:
    """Create a FastAPI router with health check endpoints.

    Provides two endpoints following Kubernetes probe best practices:

    - ``GET {prefix}/health/live``: Liveness probe. Always returns
      200 with ``{"status": "healthy"}``. Never checks dependencies.
    - ``GET {prefix}/health/ready``: Readiness probe. Runs all
      registered checkers concurrently. Returns 200 if all healthy,
      503 if any component is degraded.

    Uses ``orjson`` for JSON serialization when available, falling
    back to the standard library ``json`` module.

    Raises:
        DependencyNotFoundError: If ``fastapi`` is not installed.
    """
    try:
        from fastapi import APIRouter as _APIRouter  # noqa: PLC0415
        from fastapi.responses import Response  # noqa: PLC0415
        from starlette.status import (  # noqa: PLC0415
            HTTP_200_OK,
            HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ImportError:
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")  # noqa: B904

    _liveness_body = json_dumps_bytes({"status": "healthy"})

    router = _APIRouter(prefix=prefix, tags=["health"])

    @router.get("/health/live", response_model=LivenessResponse)
    async def liveness() -> Response:
        """Liveness probe. Always healthy if the process is running."""
        return Response(
            content=_liveness_body,
            media_type="application/json",
        )

    @router.get(
        "/health/ready",
        response_model=ReadinessResponse,
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "model": ReadinessResponse,
                "description": "At least one component is unhealthy.",
            },
        },
    )
    async def readiness() -> Response:
        """Readiness probe. Checks all registered health checkers."""
        report = await registry.check()
        status_code = (
            HTTP_200_OK
            if report["status"] == OverallStatus.HEALTHY
            else HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(
            content=json_dumps_bytes(report),
            status_code=status_code,
            media_type="application/json",
        )

    return router
