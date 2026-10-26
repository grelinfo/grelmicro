"""Type assertions for the arguments that pick a mode.

Each one takes the documented string with nothing to import, and a string
outside the choices fails the type check.
"""

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
    configure(backend="loguru", level="DEBUG", format="json")
    Log(backend="stdlib", level="INFO")
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


def log_format_spellings() -> None:
    """Annotate a format in the documented spelling or in uppercase."""
    documented: LogFormat = "json"
    uppercase: LogFormat = "JSON"
    configure(format=documented)
    configure(format=uppercase)


def log_format_unknown() -> None:
    """Refuse a format name in neither spelling."""
    mixed: LogFormat = "Json"  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    configure(format=mixed)
