"""Tracing Configuration."""

from enum import StrEnum
from typing import Self

from pydantic_settings import BaseSettings


class _CaseInsensitiveEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        value = str(value).lower()
        for member in cls:
            if member.lower() == value:
                return member
        return None


class TracingExporterType(_CaseInsensitiveEnum):
    """Tracing Exporter Enum."""

    OTLP = "otlp"
    CONSOLE = "console"
    NONE = "none"


class TracingSettings(BaseSettings):
    """Tracing Settings.

    Environment Variables:
        TRACING_ENABLED: Enable distributed tracing. Default: False
        TRACING_EXPORTER: Span exporter type (otlp, console, none).
            Default: otlp
        OTEL_SERVICE_NAME: OpenTelemetry service name.
            Default: unknown_service
    """

    TRACING_ENABLED: bool = False
    TRACING_EXPORTER: TracingExporterType = TracingExporterType.OTLP
    OTEL_SERVICE_NAME: str = "unknown_service"
