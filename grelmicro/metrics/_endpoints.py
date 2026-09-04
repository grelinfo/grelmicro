"""Metrics endpoint, served without a web framework.

One implementation of what `/metrics` answers. The FastAPI router, the ASGI
app, and `OpsServer` all render through here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

from typing_extensions import Doc

from grelmicro._endpoints import HTTP_OK, Rendered, build_asgi
from grelmicro.metrics.config import MetricsExporterType
from grelmicro.metrics.errors import MetricsError

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from grelmicro._endpoints import ASGIApp, Handler
    from grelmicro.metrics._component import Metrics

    Scope = MutableMapping[str, Any]

__all__ = ["metrics_asgi"]

PROMETHEUS_MEDIA_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
"""Content type of the Prometheus text exposition format, version 0.0.4."""


def render_prometheus(component: Metrics) -> bytes:
    """Render the exposition of the component's collector registry.

    Raises:
        MetricsError: If the component does not use the `prometheus`
            exporter, so there is no registry to render.
    """
    from prometheus_client import generate_latest  # noqa: PLC0415

    if component.config.exporter != MetricsExporterType.PROMETHEUS:
        msg = (
            "the /metrics endpoint requires the prometheus exporter, but "
            f"the active Metrics component uses {component.config.exporter!r}."
            " Set exporter='prometheus' to expose /metrics."
        )
        raise MetricsError(msg)
    registry = component.prometheus_registry
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
    return generate_latest(registry)


def metrics_routes(
    component: Metrics | None = None,
    *,
    prefix: str = "",
    path: str = "/metrics",
) -> dict[str, Handler]:
    """Build the metrics handler, keyed by the path it answers."""

    def resolve() -> Metrics:
        if component is not None:
            return component
        from grelmicro._app import Grelmicro  # noqa: PLC0415

        return Grelmicro.current().get("metrics", "default")

    async def metrics(_scope: Scope) -> Rendered:
        """Render the Prometheus exposition for the active registry."""
        return Rendered(
            HTTP_OK, render_prometheus(resolve()), PROMETHEUS_MEDIA_TYPE
        )

    return {f"{prefix}{path}": metrics}


def metrics_asgi(
    component: Annotated[
        Metrics | None,
        Doc(
            "Metrics component whose Prometheus registry the endpoint "
            "renders. When omitted, it resolves the default instance from "
            "the active `Grelmicro` app (``Grelmicro(uses=[Metrics(...)])``)."
        ),
    ] = None,
    *,
    prefix: Annotated[
        str,
        Doc(
            "URL prefix for the endpoint. Leave it empty when the app is "
            "mounted, because the mount strips its own path first."
        ),
    ] = "",
    path: Annotated[
        str,
        Doc("Path of the metrics endpoint under the prefix."),
    ] = "/metrics",
) -> ASGIApp:
    """Create a pure-ASGI app serving the Prometheus endpoint.

    The endpoint [`metrics_router`][grelmicro.metrics.metrics_router]
    serves, rendered by the same code, with no framework anywhere.
    ``GET/HEAD {prefix}{path}`` returns the Prometheus exposition of the
    component's collector registry, with ``Cache-Control: no-store``. Any
    other path answers ``404``, and any other method ``405``.

    Mount it in an ASGI framework:

    ```python
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from grelmicro.metrics import metrics_asgi

    app = Starlette(routes=[Mount("", app=metrics_asgi())])
    ```

    Or give it a port of its own with
    [`OpsServer`][grelmicro.http.OpsServer], for a worker that runs no web
    framework at all.

    On FastAPI, prefer `metrics_router()`: it serves the same endpoint and
    adds the OpenAPI schema and the `Depends` gate.
    """
    return build_asgi(metrics_routes(component, prefix=prefix, path=path))
