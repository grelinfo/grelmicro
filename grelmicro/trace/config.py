"""Tracing Configuration."""

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


class TracingExporterType(_CaseInsensitiveEnum):
    """Span exporter selection."""

    OTLP_HTTP = "otlp-http"
    OTLP_GRPC = "otlp-grpc"
    CONSOLE = "console"
    NONE = "none"


class TracingProcessorType(_CaseInsensitiveEnum):
    """Span processor selection."""

    BATCH = "batch"
    SIMPLE = "simple"


class TracingSamplerType(_CaseInsensitiveEnum):
    """Sampler selection."""

    ALWAYS_ON = "always_on"
    ALWAYS_OFF = "always_off"
    PARENTBASED_ALWAYS_ON = "parentbased_always_on"
    TRACEIDRATIO = "traceidratio"


class TracingConfig(BaseModel, frozen=True, extra="forbid"):
    """Tracing Config."""

    service_name: Annotated[
        str | None,
        Doc(
            "Service name resource attribute. Falls back to "
            "`OTEL_SERVICE_NAME` when unset."
        ),
    ] = None
    exporter: Annotated[
        TracingExporterType,
        Doc("Span exporter."),
    ] = TracingExporterType.OTLP_HTTP
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
    processor: Annotated[
        TracingProcessorType,
        Doc("Span processor."),
    ] = TracingProcessorType.BATCH
    sampler: Annotated[
        TracingSamplerType,
        Doc("Sampler."),
    ] = TracingSamplerType.PARENTBASED_ALWAYS_ON
    sample_ratio: Annotated[
        float,
        Doc("Sample ratio for `traceidratio` sampler."),
        Field(ge=0.0, le=1.0),
    ] = 1.0
    resource_attributes: Annotated[
        dict[str, str],
        Doc("Extra resource attributes."),
    ] = Field(default_factory=dict)
    shutdown_timeout: Annotated[
        PositiveFloat,
        Doc(
            "Maximum seconds to wait for the `TracerProvider.shutdown()` "
            "flush. A slow or broken exporter no longer hangs application "
            "shutdown past this deadline."
        ),
    ] = 5.0
