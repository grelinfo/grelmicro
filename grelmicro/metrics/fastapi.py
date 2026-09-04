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
    include_in_schema: Annotated[
        bool,
        Doc(
            "Whether the endpoint appears in the OpenAPI schema:\n\n"
            "- ``False`` (default): served, but absent from "
            "``/openapi.json`` and the docs pages.\n"
            "- ``True``: documented as returning the Prometheus "
            "exposition, which is text and not JSON.\n\n"
            "The endpoint answers the same either way. This decides "
            "what the schema publishes, not what is reachable."
        ),
    ] = False,
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
    The endpoint stays out of the OpenAPI schema unless
    ``include_in_schema=True``.

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

    router = _APIRouter(
        prefix=prefix, tags=["metrics"], include_in_schema=include_in_schema
    )
    deps = list(dependencies or ())

    @router.get(
        path,
        dependencies=deps,
        response_class=Response,
        responses={
            200: {
                "description": "Prometheus exposition of the active registry.",
                "content": {
                    _PROMETHEUS_CONTENT_TYPE: {"schema": {"type": "string"}}
                },
            },
        },
    )
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
        registry = active.prometheus_registry
        if registry is None:  # pragma: no cover
            # Unreachable while the exporter is `prometheus`, which the check
            # above already enforces. Stated rather than assumed, so a future
            # exporter that forgets to build a registry fails here with a
            # sentence instead of inside `generate_latest`.
            msg = (
                "the prometheus exporter is active but no CollectorRegistry "
                "was built, so /metrics has nothing to render."
            )
            raise MetricsError(msg)
        return Response(
            content=generate_latest(registry),
            media_type=_PROMETHEUS_CONTENT_TYPE,
        )

    return router
