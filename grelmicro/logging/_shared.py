"""Shared logging utilities."""

import json
from collections.abc import Callable, Mapping
from datetime import tzinfo
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError
from grelmicro.logging.config import (
    LoggingFormatType,
    LoggingSerializerType,
    LoggingSettings,
)
from grelmicro.logging.errors import LoggingSettingsValidationError

try:
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace: Any = None

try:
    import orjson
except ImportError:  # pragma: no cover
    orjson: Any = None


def _stdlib_json_dumps(obj: Mapping[str, Any]) -> str:
    """Serialize object to JSON string using stdlib json."""
    return json.dumps(obj, separators=(",", ":"))


def _orjson_dumps(obj: Mapping[str, Any]) -> str:
    """Serialize object to JSON string using orjson.

    Note: Only called when orjson is available (validated by load_settings).
    """
    return orjson.dumps(obj).decode("utf-8")  # type: ignore[union-attr]


def has_orjson() -> bool:
    """Check if orjson is available."""
    return orjson is not None


def has_opentelemetry() -> bool:
    """Check if OpenTelemetry is available."""
    return trace is not None


def get_otel_trace_context() -> dict[str, str]:
    """Extract OpenTelemetry trace context from current span.

    Returns:
        Dictionary with trace_id and span_id if active span exists,
        otherwise empty dictionary.
    """
    if not trace:
        return {}

    span = trace.get_current_span()
    span_context = span.get_span_context()

    if not span_context.is_valid:
        return {}

    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
    }


class LoadedSettings(NamedTuple):
    """Validated logging settings."""

    settings: LoggingSettings
    timezone: tzinfo
    use_json: bool
    json_dumps: Callable[[Mapping[str, Any]], str]


def load_settings() -> LoadedSettings:
    """Load and validate logging settings.

    Raises:
        DependencyNotFoundError: If orjson or OpenTelemetry is enabled but not installed.
        LoggingSettingsValidationError: If environment variables are invalid.
    """
    try:
        settings = LoggingSettings()
    except ValidationError as error:
        raise LoggingSettingsValidationError(error) from None

    if settings.LOG_JSON_SERIALIZER == LoggingSerializerType.ORJSON:
        if not has_orjson():
            raise DependencyNotFoundError(module="orjson")
        json_dumps = _orjson_dumps
    else:
        json_dumps = _stdlib_json_dumps

    if settings.LOG_OTEL_ENABLED and not has_opentelemetry():
        raise DependencyNotFoundError(module="opentelemetry")

    timezone = ZoneInfo(str(settings.LOG_TIMEZONE))
    use_json = settings.LOG_FORMAT == LoggingFormatType.JSON

    return LoadedSettings(
        settings=settings,
        timezone=timezone,
        use_json=use_json,
        json_dumps=json_dumps,
    )
