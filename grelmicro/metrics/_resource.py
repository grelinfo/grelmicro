"""Resource construction shared by the metrics pipeline.

Mirrors the resource logic in `grelmicro.trace._component`: merge any
explicit `resource_attributes` with the `service.name` derived from
`service_name`, and build an OTel `Resource` only when there is at least
one attribute to set.
"""

from __future__ import annotations

from typing import Any

from grelmicro.errors import DependencyNotFoundError


def build_resource(
    *,
    service_name: str | None,
    resource_attributes: dict[str, str],
) -> Any:  # noqa: ANN401
    """Build an OTel `Resource`, or `None` when no attributes are set."""
    try:
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise DependencyNotFoundError(module="opentelemetry-sdk") from exc

    resource_attrs: dict[str, Any] = dict(resource_attributes)
    if service_name is not None:
        resource_attrs["service.name"] = service_name
    return Resource.create(resource_attrs) if resource_attrs else None
