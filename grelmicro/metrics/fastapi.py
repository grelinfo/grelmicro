"""FastAPI Metrics Router."""

from typing import TYPE_CHECKING, Annotated

from typing_extensions import Doc

from grelmicro._endpoints import NO_STORE_HEADERS
from grelmicro.metrics._component import Metrics
from grelmicro.metrics._endpoints import (
    PROMETHEUS_MEDIA_TYPE,
    render_prometheus,
)

if TYPE_CHECKING:
    from fastapi import APIRouter
    from fastapi.params import Depends


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
                    PROMETHEUS_MEDIA_TYPE: {"schema": {"type": "string"}}
                },
            },
        },
    )
    async def metrics() -> Response:
        """Render the Prometheus exposition for the active registry."""
        return Response(
            content=render_prometheus(_resolve_component()),
            media_type=PROMETHEUS_MEDIA_TYPE,
            headers=NO_STORE_HEADERS,
        )

    return router
