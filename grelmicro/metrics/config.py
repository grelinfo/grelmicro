"""Metrics Configuration."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, PositiveFloat
from typing_extensions import Doc


class _CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        value = value.lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class MetricsExporterType(_CaseInsensitiveEnum):
    """Metric exporter selection."""

    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    PROMETHEUS = "prometheus"
    CONSOLE = "console"
    NONE = "none"


class MetricsConfig(BaseModel, frozen=True, extra="forbid"):
    """Metrics Config."""

    service_name: Annotated[
        str | None,
        Doc(
            "Service name resource attribute. Falls back to "
            "`OTEL_SERVICE_NAME` when unset."
        ),
    ] = None
    exporter: Annotated[
        MetricsExporterType,
        Doc("Metric exporter."),
    ] = MetricsExporterType.OTLP_HTTP
    endpoint: Annotated[
        str | None,
        Doc(
            "Exporter endpoint. Falls back to `OTEL_EXPORTER_OTLP_ENDPOINT` "
            "when unset."
        ),
    ] = None
    headers: Annotated[
        dict[str, str],
        Doc(
            "Exporter headers. Falls back to `OTEL_EXPORTER_OTLP_HEADERS` "
            "when empty."
        ),
    ] = Field(default_factory=dict)
    export_interval: Annotated[
        PositiveFloat,
        Doc(
            "Seconds between exports for the periodic reader. Applies to "
            "the OTLP and console exporters. The Prometheus exporter is "
            "pull-based and ignores this value."
        ),
    ] = 60.0
    export_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Maximum seconds a single periodic export may take before it "
            "is abandoned."
        ),
    ] = 30.0
    resource_attributes: Annotated[
        dict[str, str],
        Doc("Extra resource attributes."),
    ] = Field(default_factory=dict)
    shutdown_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Maximum seconds to wait for the `MeterProvider.shutdown()` "
            "flush. A slow or broken exporter no longer hangs application "
            "shutdown past this deadline."
        ),
    ] = 5.0
