"""Logging."""

from typing import Annotated

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.log._dedup import DuplicateFilter, DuplicateFilterConfig
from grelmicro.log._ratelimit import (
    RateLimitFilter,
    RateLimitFilterConfig,
)
from grelmicro.log.config import (
    LoggingBackendType,
    LoggingConfig,
    LoggingFormatType,
    LoggingLevelType,
    LoggingSerializerType,
    LoggingTimeZoneType,
)
from grelmicro.log.errors import LoggingError
from grelmicro.log.types import ErrorDict, JSONRecordDict


def _apply(config: LoggingConfig) -> None:
    """Dispatch to the selected backend with the resolved config."""
    if config.backend == LoggingBackendType.STRUCTLOG:
        from grelmicro.log._structlog import (  # noqa: PLC0415
            configure as _configure,
        )
    elif config.backend == LoggingBackendType.STDLIB:
        from grelmicro.log._stdlib import (  # noqa: PLC0415
            configure as _configure,
        )
    else:
        from grelmicro.log._loguru import (  # noqa: PLC0415
            configure as _configure,
        )

    _configure(config)


def configure(
    *,
    backend: Annotated[
        LoggingBackendType | None,
        Doc(
            "Logging backend (`stdlib`, `loguru`, `structlog`). Default: `stdlib`."
        ),
    ] = None,
    level: Annotated[
        LoggingLevelType | None,
        Doc("Log level threshold. Default: `INFO`."),
    ] = None,
    format: Annotated[  # noqa: A002
        LoggingFormatType | str | None,
        Doc("Log format. Default: `AUTO`."),
    ] = None,
    timezone: Annotated[
        LoggingTimeZoneType | None,
        Doc("IANA timezone for timestamps. Default: `UTC`."),
    ] = None,
    json_serializer: Annotated[
        LoggingSerializerType | None,
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
    read_env: Annotated[
        bool | None,
        Doc(
            "Whether to read `GREL_LOG_*` environment variables. "
            "When None (default), follow `GREL_CONFIG_FROM_ENV`. "
            "Pass True or False to override."
        ),
    ] = None,
) -> LoggingConfig:
    """Configure logging with the selected backend.

    Two paths:

    - Programmatic: pass any of the per-field kwargs. Unset fields
      resolve from `GREL_LOG_*` env vars (when `read_env=True`),
      then from `LoggingConfig` defaults.
    - Environmental: omit all kwargs. `GREL_LOG_*` env vars populate
      every field.

    For the declarative path, use
    [`configure_with`][grelmicro.log.configure_with].

    Returns:
        The applied `LoggingConfig`. Snapshot of what was resolved.

    Raises:
        DependencyNotFoundError: If the selected backend module is not installed.
        pydantic.ValidationError: If configuration is invalid.
    """
    config = resolve_config(
        LoggingConfig,
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
        read_env=read_env,
    )
    _apply(config)
    return config


def configure_with(
    config: Annotated[
        LoggingConfig,
        Doc(
            """
            Pre-built logging configuration.

            Use this path when the configuration is assembled at
            startup from a settings tree. The environment path is
            bypassed and the config is used as-is.
            """
        ),
    ],
) -> LoggingConfig:
    """Configure logging from a pre-built `LoggingConfig`.

    Returns:
        The same `LoggingConfig`, for symmetry with `configure`.
    """
    _apply(config)
    return config


__all__ = [
    "DuplicateFilter",
    "DuplicateFilterConfig",
    "ErrorDict",
    "JSONRecordDict",
    "LoggingConfig",
    "LoggingError",
    "RateLimitFilter",
    "RateLimitFilterConfig",
    "configure",
    "configure_with",
]
