"""FastAPI Metrics Router."""

from typing import TYPE_CHECKING, Annotated

from typing_extensions import Doc

from grelmicro.metrics._component import Metrics
from grelmicro.metrics.config import MetricsExporterType
from grelmicro.metrics.errors import MetricsError

if TYPE_CHECKING:
    from fastapi import APIRouter
    from fastapi.params import Depends


_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def metrics_router(
    component: Annotated[
        Metrics | None,
        Doc(
            "Metrics component whose Prometheus registry the endpoint "
            "renders. When omitted, the router resolves the default "
            "instance from the active `Grelmicro` app "
            "(``Grelmicro(uses=[Metrics(...)])``)."
        ),
    ] = None,
    *,
    prefix: Annotated[
        str,
        Doc("URL prefix for the metrics endpoint (e.g. '/api/v1')."),
    ] = "",
    path: Annotated[
        str,
        Doc("Path of the metrics endpoint under the prefix."),
    ] = "/metrics",
    dependencies: Annotated[
        "list[Depends] | None",
        Doc(
            "FastAPI dependencies applied to the metrics endpoint. A "
            "failing dependency blocks the endpoint (``401``/``403``). "
            "Use to gate ``/metrics`` behind authentication."
        ),
    ] = None,
) -> "APIRouter":
    """Create a FastAPI router that serves Prometheus metrics.

    Mounts ``GET {prefix}{path}`` (default ``GET /metrics``) returning the
    Prometheus exposition format rendered from the component's collector
    registry. The active component must use the ``prometheus`` exporter.

    Raises:
        DependencyNotFoundError: If ``fastapi`` is not installed.
    """
    try:
        from fastapi import APIRouter as _APIRouter  # noqa: PLC0415
        from fastapi.responses import Response  # noqa: PLC0415
    except ImportError:
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")  # noqa: B904

    from grelmicro._app import Grelmicro  # noqa: PLC0415

    def _resolve_component() -> Metrics:
        return component or Grelmicro.current().get("metrics", "default")

    router = _APIRouter(prefix=prefix, tags=["metrics"])
    deps = list(dependencies or ())

    @router.get(path, dependencies=deps)
    async def metrics() -> Response:
        """Render the Prometheus exposition for the active registry."""
        from prometheus_client import generate_latest  # noqa: PLC0415

        active = _resolve_component()
        if active.config.exporter != MetricsExporterType.PROMETHEUS:
            msg = (
                "metrics_router requires the prometheus exporter, but the "
                f"active Metrics component uses {active.config.exporter!r}. "
                "Set exporter='prometheus' to expose /metrics."
            )
            raise MetricsError(msg)
        return Response(
            content=generate_latest(active.prometheus_registry),
            media_type=_PROMETHEUS_CONTENT_TYPE,
        )

    return router
