"""Type assertions for the arguments that pick a mode.

Each one takes the documented string with nothing to import, and a string
outside the choices fails the type check.
"""

from grelmicro.log import (
    Log,
    LogBackendType,
    LogConfig,
    LogFormat,
    LogFormatType,
    LogLevelType,
    LogSerializerType,
    configure,
)
from grelmicro.metrics import (
    Metrics,
    MetricsConfig,
    MetricsExporter,
    MetricsExporterType,
)
from grelmicro.trace import (
    Trace,
    TraceConfig,
    TraceExporterType,
    TraceProcessorType,
    TraceSamplerType,
)
from grelmicro.types import LogLevel


def strings() -> None:
    """Pass the documented spelling to the component and the config."""
    Metrics(exporter="prometheus")
    MetricsConfig(exporter="otlp-grpc")
    Trace(exporter="console", processor="simple", sampler="traceidratio")
    TraceConfig(sampler="parentbased_always_on")
    configure(backend="loguru", level="DEBUG", format="JSON")
    LogConfig(json_serializer="orjson", format="{level} | {message}")


def annotated(
    exporter: MetricsExporter, level: LogLevel, fmt: LogFormat
) -> None:
    """Pass a value annotated with the alias straight through."""
    Metrics(exporter=exporter)
    configure(level=level, format=fmt)


def unknown() -> None:
    """Refuse a string outside the choices."""
    Metrics(exporter="jaeger")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def enum_members() -> None:
    """Pass a member of an old enum, accepted until 0.44.0."""
    Metrics(exporter=MetricsExporterType.PROMETHEUS)
    Trace(
        exporter=TraceExporterType.CONSOLE,
        processor=TraceProcessorType.SIMPLE,
        sampler=TraceSamplerType.TRACEIDRATIO,
    )
    configure(
        backend=LogBackendType.LOGURU,
        level=LogLevelType.DEBUG,
        format=LogFormatType.JSON,
        json_serializer=LogSerializerType.ORJSON,
    )
    Log(backend=LogBackendType.STDLIB, level=LogLevelType.INFO)
