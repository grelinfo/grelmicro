"""Type assertions for the arguments that pick a mode.

Each one takes the documented string with nothing to import, and a string
outside the choices fails the type check.
"""

import grelmicro.log
import grelmicro.metrics
import grelmicro.trace
from grelmicro.log import (
    Log,
    LogConfig,
    LogFormat,
    configure,
)
from grelmicro.metrics import (
    Metrics,
    MetricsConfig,
    MetricsExporter,
)
from grelmicro.trace import (
    Trace,
    TraceConfig,
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
    """Pass a member of an old enum, accepted until 0.44.0 and reported."""
    log = grelmicro.log
    trace = grelmicro.trace
    Metrics(
        exporter=grelmicro.metrics.MetricsExporterType.PROMETHEUS  # type: ignore[deprecated]  # ty: ignore[deprecated]
    )
    Trace(
        exporter=trace.TraceExporterType.CONSOLE,  # type: ignore[deprecated]  # ty: ignore[deprecated]
        processor=trace.TraceProcessorType.SIMPLE,  # type: ignore[deprecated]  # ty: ignore[deprecated]
        sampler=trace.TraceSamplerType.TRACEIDRATIO,  # type: ignore[deprecated]  # ty: ignore[deprecated]
    )
    configure(
        backend=log.LogBackendType.LOGURU,  # type: ignore[deprecated]  # ty: ignore[deprecated]
        level=log.LogLevelType.DEBUG,  # type: ignore[deprecated]  # ty: ignore[deprecated]
        format=log.LogFormatType.JSON,  # type: ignore[deprecated]  # ty: ignore[deprecated]
        json_serializer=log.LogSerializerType.ORJSON,  # type: ignore[deprecated]  # ty: ignore[deprecated]
    )
    Log(
        backend=log.LogBackendType.STDLIB,  # type: ignore[deprecated]  # ty: ignore[deprecated]
        level=log.LogLevelType.INFO,  # type: ignore[deprecated]  # ty: ignore[deprecated]
    )
