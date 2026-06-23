"""Logging."""

from typing import Annotated

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.log._apply import apply as _apply
from grelmicro.log._component import Log
from grelmicro.log._dedup import DuplicateFilter, DuplicateFilterConfig
from grelmicro.log._ratelimit import (
    RateLimitFilter,
    RateLimitFilterConfig,
)
from grelmicro.log.config import (
    LogBackendType,
    LogConfig,
    LogFormatType,
    LogLevelType,
    LogSerializerType,
    LogTimeZoneType,
)
from grelmicro.log.errors import LogError, LogSettingsValidationError
from grelmicro.log.types import ErrorDict, JSONRecordDict


def configure(
    *,
    backend: Annotated[
        LogBackendType | None,
        Doc(
            "Logging backend (`stdlib`, `loguru`, `structlog`). Default: `stdlib`."
        ),
    ] = None,
    level: Annotated[
        LogLevelType | None,
        Doc("Log level threshold. Default: `INFO`."),
    ] = None,
    format: Annotated[  # noqa: A002
        LogFormatType | str | None,
        Doc("Log format. Default: `AUTO`."),
    ] = None,
    timezone: Annotated[
        LogTimeZoneType | None,  # ty: ignore[invalid-type-form]
        Doc("IANA timezone for timestamps. Default: `UTC`."),
    ] = None,
    json_serializer: Annotated[
        LogSerializerType | None,
        Doc("JSON serializer. Default: `stdlib`."),
    ] = None,
    caller_enabled: Annotated[
        bool | None,
        Doc(
            "Include caller (function and line) in log records. Default: False."
        ),
    ] = None,
    otel_enabled: Annotated[
        bool | None,
        Doc(
            """
            Extract OpenTelemetry trace context.

            Default: True if OpenTelemetry is installed, else False.
            """
        ),
    ] = None,
    env_load: Annotated[
        bool | None,
        Doc(
            "Whether to read `GREL_LOG_*` environment variables. "
            "When None (default), follow `GREL_ENV_LOAD`. "
            "Pass True or False to override."
        ),
    ] = None,
) -> LogConfig:
    """Configure logging with the selected backend.

    Two paths:

    - Programmatic: pass any of the per-field kwargs. Unset fields
      resolve from `GREL_LOG_*` env vars (when `env_load=True`),
      then from `LogConfig` defaults.
    - Environmental: omit all kwargs. `GREL_LOG_*` env vars populate
      every field.

    For the declarative path, use
    [`configure_with`][grelmicro.log.configure_with].

    Returns:
        The applied `LogConfig`. Snapshot of what was resolved.

    Raises:
        DependencyNotFoundError: If the selected backend module is not installed.
        LogSettingsValidationError: If configuration is invalid.
    """
    config = resolve_config(
        LogConfig,
        explicit=None,
        kwargs={
            "backend": backend,
            "level": level,
            "format": format,
            "timezone": timezone,
            "json_serializer": json_serializer,
            "caller_enabled": caller_enabled,
            "otel_enabled": otel_enabled,
        },
        env_prefix="GREL_LOG_",
        env_load=env_load,
        error_type=LogSettingsValidationError,
    )
    _apply(config)
    return config


def configure_with(
    config: Annotated[
        LogConfig,
        Doc(
            """
            Pre-built logging configuration.

            Use this path when the configuration is assembled at
            startup from a settings tree. The environment path is
            bypassed and the config is used as-is.
            """
        ),
    ],
) -> LogConfig:
    """Configure logging from a pre-built `LogConfig`.

    Returns:
        The same `LogConfig`, for symmetry with `configure`.
    """
    _apply(config)
    return config


__all__ = [
    "DuplicateFilter",
    "DuplicateFilterConfig",
    "ErrorDict",
    "JSONRecordDict",
    "Log",
    "LogConfig",
    "LogError",
    "LogSettingsValidationError",
    "RateLimitFilter",
    "RateLimitFilterConfig",
    "configure",
    "configure_with",
]
