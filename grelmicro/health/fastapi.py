"""FastAPI Health Check Router."""

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.health._models import HealthReport, HealthStatus, OverallStatus

if TYPE_CHECKING:
    from fastapi import APIRouter


class ComponentHealthResponse(BaseModel):
    """Health status of a single component."""

    name: str
    status: HealthStatus
    critical: bool = True
    error: str | None = None
    details: dict[str, Any] | None = None


class LivenessResponse(BaseModel):
    """Liveness probe response.

    Always returns ``"healthy"``. If the process can serve this
    response it is alive; if it cannot, the orchestrator (e.g.
    Kubernetes, Nomad, or a load balancer) will detect the failure
    and restart the instance. Liveness probes must never check
    dependencies (that is the readiness probe's job).
    """

    status: Literal["healthy"] = "healthy"


class ReadinessResponse(BaseModel):
    """Readiness probe response."""

    status: OverallStatus
    components: list[ComponentHealthResponse]


def health_router(
    *,
    prefix: Annotated[
        str,
        Doc("URL prefix for health endpoints (e.g. '/api')."),
    ] = "",
    show_details: Annotated[
        bool,
        Doc(
            "Include checker details in the readiness response. "
            "When False (default), the ``details`` field is stripped "
            "from each component. Can be overridden per-request with "
            "the ``?details=true`` query parameter."
        ),
    ] = False,
) -> "APIRouter":
    """Create a FastAPI router with health check endpoints.

    Provides two endpoints following standard liveness/readiness
    probe conventions (used by Kubernetes, Nomad, Consul, load
    balancers, etc.):

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
        from fastapi import Query  # noqa: PLC0415
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

    from grelmicro.health._state import get_health_registry  # noqa: PLC0415

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
    async def readiness(
        details: Annotated[
            bool,
            Query(description="Include checker details in the response."),
        ] = show_details,
    ) -> Response:
        """Readiness probe. Checks all registered health checkers."""
        report = await get_health_registry().check()
        if not details:
            report = _strip_details(report)
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


def _strip_details(report: HealthReport) -> dict[str, Any]:
    """Remove the details field from each component."""
    return {
        "status": report["status"],
        "components": [
            {k: v for k, v in component.items() if k != "details"}
            for component in report["components"]
        ],
    }
