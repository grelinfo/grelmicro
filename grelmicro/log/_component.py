"""Log component for the Grelmicro app object."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.log._apply import apply as _apply
from grelmicro.log.config import (
    LogBackendType,
    LogConfig,
    LogFormatType,
    LogLevelType,
    LogSerializerType,
    LogTimeZoneType,
)
from grelmicro.log.errors import LogSettingsValidationError

if TYPE_CHECKING:
    from types import TracebackType


class Log:
    """Log component: installs logging on enter, restores stdlib root state on exit.

    Registered as `micro.log` after `Grelmicro.use(Log(...))`. Mirrors the
    knobs on `grelmicro.log.configure(...)`. Construction stays cheap, the
    backend is configured when the surrounding `Grelmicro` opens.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.log import Log

        micro = Grelmicro(uses=[Log()])

        async with micro:
            ...
        ```

    On exit, the previous stdlib root handlers and level are restored so
    sequential `Grelmicro(...)` blocks do not pile handlers up. The
    `loguru` and `structlog` backends keep the configuration installed on
    enter (no restore).

    The stdlib root logger is a single global. `Log.__aenter__` and
    `Log.__aexit__` serialize on a class-level `threading.Lock` so the
    snapshot/restore sequence cannot interleave across concurrent
    `Grelmicro` lifecycles in the same process. Run one `Log` at a
    time per process.

    Read more in the [Logging](../logging.md) docs.
    """

    kind: ClassVar[str] = "log"
    singleton: ClassVar[bool] = True
    _lifecycle_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. `Log` configures the process-wide root
                logger, so only one may be registered per app.
                """
            ),
        ] = "default",
        config: Annotated[
            LogConfig | None,
            Doc(
                """
                Pre-built configuration. When provided, individual kwargs
                must be `None`. The env path is bypassed.
                """
            ),
        ] = None,
        backend: Annotated[
            LogBackendType | None,
            Doc("Logging backend (`stdlib`, `loguru`, `structlog`)."),
        ] = None,
        level: Annotated[
            LogLevelType | None, Doc("Log level threshold.")
        ] = None,
        format: Annotated[  # noqa: A002
            LogFormatType | str | None, Doc("Log format.")
        ] = None,
        timezone: Annotated[
            LogTimeZoneType | None,  # ty: ignore[invalid-type-form]
            Doc("IANA timezone for timestamps."),
        ] = None,
        json_serializer: Annotated[
            LogSerializerType | None, Doc("JSON serializer.")
        ] = None,
        caller_enabled: Annotated[
            bool | None,
            Doc("Include caller (function and line) in log records."),
        ] = None,
        otel_enabled: Annotated[
            bool | None, Doc("Extract OpenTelemetry trace context.")
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read `GREL_LOG_*` environment variables. "
                "When None (default), follow `GREL_ENV_LOAD`."
            ),
        ] = None,
    ) -> None:
        """Initialize the component (defer configuration until `__aenter__`)."""
        self._name = name
        self._explicit_config = config
        self._kwargs = {
            "backend": backend,
            "level": level,
            "format": format,
            "timezone": timezone,
            "json_serializer": json_serializer,
            "caller_enabled": caller_enabled,
            "otel_enabled": otel_enabled,
        }
        self._env_load = env_load
        self._resolved: LogConfig | None = None
        self._snapshot_handlers: list[logging.Handler] | None = None
        self._snapshot_level: int | None = None

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            LogConfig,
            Doc(
                """
                The pre-built logging configuration.

                Use this path when the configuration is assembled at
                startup from a settings tree (for example YAML, Vault,
                or a `pydantic-settings` aggregator). The environment
                path is bypassed and the config is used as-is.
                """,
            ),
        ],
        *,
        name: Annotated[
            str,
            Doc("Registration name. Defaults to `'default'`."),
        ] = "default",
    ) -> Self:
        """Construct a `Log` from a pre-built `LogConfig`."""
        return cls(name=name, config=config)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def config(self) -> LogConfig:
        """Return the resolved `LogConfig`.

        Raises:
            RuntimeError: If accessed before the component has been entered.
        """
        if self._resolved is None:
            msg = "Log.config is only available inside `async with micro:`"
            raise RuntimeError(msg)
        return self._resolved

    async def __aenter__(self) -> Self:
        """Snapshot stdlib root logger state, then configure logging."""
        with self._lifecycle_lock:
            root = logging.getLogger()
            self._snapshot_handlers = list(root.handlers)
            self._snapshot_level = root.level
            self._resolved = resolve_config(
                LogConfig,
                explicit=self._explicit_config,
                kwargs=self._kwargs,
                env_prefix="GREL_LOG_",
                env_load=self._env_load,
                error_type=LogSettingsValidationError,
            )
            _apply(self._resolved)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Restore the snapshotted stdlib root handlers and level."""
        with self._lifecycle_lock:
            root = logging.getLogger()
            for handler in list(root.handlers):
                root.removeHandler(handler)
            if self._snapshot_handlers is not None:  # pragma: no branch
                for handler in self._snapshot_handlers:
                    root.addHandler(handler)
            if self._snapshot_level is not None:  # pragma: no branch
                root.setLevel(self._snapshot_level)
            self._snapshot_handlers = None
            self._snapshot_level = None
        return None
