"""Tracing."""

from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError
from grelmicro.tracing.config import TracingExporterType, TracingSettings
from grelmicro.tracing.errors import (
    TracingError,
    TracingSettingsValidationError,
)


def configure_tracing() -> None:
    """Configure distributed tracing with OpenTelemetry.

    Sets up a TracerProvider with the selected exporter. Spans created
    via the ``@instrument`` decorator or manually via OpenTelemetry
    will be exported accordingly.

    Environment Variables:
        TRACING_ENABLED: Enable distributed tracing. Default: False
        TRACING_EXPORTER: Span exporter (otlp, console, none).
            Default: otlp
        OTEL_SERVICE_NAME: Service name for traces.
            Default: unknown_service

    Raises:
        DependencyNotFoundError: If OpenTelemetry SDK is not installed.
        TracingSettingsValidationError: If environment variables are
            invalid.
    """
    try:
        settings = TracingSettings()
    except ValidationError as error:
        raise TracingSettingsValidationError(error) from None

    if not settings.TRACING_ENABLED:
        return

    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.sdk.resources import (  # noqa: PLC0415
            Resource,
        )
        from opentelemetry.sdk.trace import (  # noqa: PLC0415
            TracerProvider,
        )
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
    except ImportError:
        raise DependencyNotFoundError(module="opentelemetry") from None

    resource = Resource.create(
        {"service.name": settings.OTEL_SERVICE_NAME}
    )
    provider = TracerProvider(resource=resource)

    if settings.TRACING_EXPORTER == TracingExporterType.OTLP:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415, E501
                OTLPSpanExporter,
            )
        except ImportError:
            raise DependencyNotFoundError(
                module="opentelemetry-exporter-otlp"
            ) from None

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter())
        )
    elif settings.TRACING_EXPORTER == TracingExporterType.CONSOLE:
        provider.add_span_processor(
            BatchSpanProcessor(ConsoleSpanExporter())
        )

    trace.set_tracer_provider(provider)


def configure_observability() -> None:
    """Configure logging and tracing together.

    Convenience function that sets up both logging and distributed
    tracing in a single call. Logging is always configured; tracing
    is configured only when ``TRACING_ENABLED=true``.

    See ``configure_logging`` and ``configure_tracing`` for the full
    list of environment variables.

    Raises:
        DependencyNotFoundError: If a required dependency is not
            installed.
        LoggingSettingsValidationError: If logging environment
            variables are invalid.
        TracingSettingsValidationError: If tracing environment
            variables are invalid.
    """
    from grelmicro.logging import configure_logging  # noqa: PLC0415

    configure_logging()
    configure_tracing()


__all__ = [
    "TracingError",
    "TracingSettingsValidationError",
    "configure_observability",
    "configure_tracing",
    "instrument",
]


def __getattr__(name: str) -> object:
    if name == "instrument":
        from grelmicro.tracing._instrument import (  # noqa: PLC0415
            instrument,
        )

        return instrument
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
