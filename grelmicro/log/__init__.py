"""Logging."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro._timezone import SHARED_TIMEZONE_ENV
from grelmicro.log._access import (
    AccessLog,
    AccessLogConfig,
    AccessLogMiddleware,
)
from grelmicro.log._apply import apply as _apply
from grelmicro.log._component import Log
from grelmicro.log._dedup import DuplicateFilter, DuplicateFilterConfig
from grelmicro.log._dictconfig import dict_config, dict_config_with
from grelmicro.log._probes import ProbeFilter
from grelmicro.log._ratelimit import (
    RateLimitFilter,
    RateLimitFilterConfig,
)
from grelmicro.log._stdlib import formatter, handler
from grelmicro.log.config import (
    LogBackend,
    LogConfig,
    LogFormat,
    LogSerializer,
)
from grelmicro.log.errors import LogError
from grelmicro.log.types import ErrorDict, JSONRecordDict
from grelmicro.types import LogLevel, TimeZoneName

if TYPE_CHECKING:
    from grelmicro.log.config import (
        LogBackendType as LogBackendType,
    )
    from grelmicro.log.config import (
        LogFormatType as LogFormatType,
    )
    from grelmicro.log.config import (
        LogLevelType as LogLevelType,
    )
    from grelmicro.log.config import (
        LogSerializerType as LogSerializerType,
    )

_MODE_ENUMS = frozenset(
    {"LogBackendType", "LogFormatType", "LogLevelType", "LogSerializerType"}
)
"""The mode enums removed in 0.44.0."""


def configure(  # noqa: PLR0913
    *,
    backend: Annotated[
        LogBackend | LogBackendType | None,
        Doc(
            "Logging backend (`stdlib`, `loguru`, `structlog`). Default: `stdlib`."
        ),
    ] = None,
    level: Annotated[
        LogLevel | LogLevelType | None,
        Doc("Log level threshold. Default: `INFO`."),
    ] = None,
    format: Annotated[  # noqa: A002
        LogFormat | str | None,
        Doc("Log format. Default: `AUTO`."),
    ] = None,
    timezone: Annotated[
        TimeZoneName | None,
        Doc("IANA timezone for timestamps. Default: `UTC`."),
    ] = None,
    json_serializer: Annotated[
        LogSerializer | LogSerializerType | None,
        Doc(
            "JSON serializer. Default: `auto`, which uses orjson when it "
            "is installed and the standard library otherwise."
        ),
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
    uvicorn_enabled: Annotated[
        bool | None,
        Doc(
            """
            Take over uvicorn's own loggers so they match this format.

            Default: True. Pass False when something other than grelmicro
            owns uvicorn's logging. Leave it on when the server was given
            [`dict_config()`][grelmicro.log.dict_config], which is how the
            access handler follows the stream this call installs.
            """
        ),
    ] = None,
    queue_enabled: Annotated[
        bool | None,
        Doc(
            """
            Hand log writes to a background thread.

            Default: False. Turn it on when the sink is slow enough that
            a write shows up as request latency.
            """
        ),
    ] = None,
    queue_size: Annotated[
        int | None,
        Doc(
            "How many rendered records the queue holds before it starts "
            "dropping. Default: 10000."
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
        SettingsValidationError: If configuration is invalid.
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
            "uvicorn_enabled": uvicorn_enabled,
            "queue_enabled": queue_enabled,
            "queue_size": queue_size,
        },
        env_prefix="GREL_LOG_",
        shared_env=SHARED_TIMEZONE_ENV,
        env_load=env_load,
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
    "AccessLog",
    "AccessLogConfig",
    "AccessLogMiddleware",
    "DuplicateFilter",
    "DuplicateFilterConfig",
    "ErrorDict",
    "JSONRecordDict",
    "Log",
    "LogBackend",
    "LogBackendType",
    "LogConfig",
    "LogError",
    "LogFormat",
    "LogFormatType",
    "LogLevelType",
    "LogSerializer",
    "LogSerializerType",
    "ProbeFilter",
    "RateLimitFilter",
    "RateLimitFilterConfig",
    "configure",
    "configure_with",
    "dict_config",
    "dict_config_with",
    "formatter",
    "handler",
]


def __getattr__(name: str) -> object:
    """Return a mode enum removed in 0.44.0, with a warning."""
    if name in _MODE_ENUMS:
        from grelmicro._mode_enums import deprecated_enum  # noqa: PLC0415

        return deprecated_enum(name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
