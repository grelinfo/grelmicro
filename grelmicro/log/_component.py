"""Log component for the Grelmicro app object."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from typing_extensions import Doc

from grelmicro._config import resolve_config
from grelmicro.log._apply import apply as _apply
from grelmicro.log.config import (
    LoggingBackendType,
    LoggingConfig,
    LoggingFormatType,
    LoggingLevelType,
    LoggingSerializerType,
    LoggingTimeZoneType,
)

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
    _lifecycle_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc(
                """
                Registration name. Multiple `Log` components may coexist on
                one `Grelmicro` under different names.
                """
            ),
        ] = "default",
        config: Annotated[
            LoggingConfig | None,
            Doc(
                """
                Pre-built configuration. When provided, individual kwargs
                must be `None`. The env path is bypassed.
                """
            ),
        ] = None,
        backend: Annotated[
            LoggingBackendType | None,
            Doc("Logging backend (`stdlib`, `loguru`, `structlog`)."),
        ] = None,
        level: Annotated[
            LoggingLevelType | None, Doc("Log level threshold.")
        ] = None,
        format: Annotated[  # noqa: A002
            LoggingFormatType | str | None, Doc("Log format.")
        ] = None,
        timezone: Annotated[
            LoggingTimeZoneType | None,
            Doc("IANA timezone for timestamps."),
        ] = None,
        json_serializer: Annotated[
            LoggingSerializerType | None, Doc("JSON serializer.")
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
        self.name = name
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
        self._resolved: LoggingConfig | None = None
        self._snapshot_handlers: list[logging.Handler] | None = None
        self._snapshot_level: int | None = None

    @property
    def config(self) -> LoggingConfig:
        """Return the resolved `LoggingConfig`.

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
                LoggingConfig,
                explicit=self._explicit_config,
                kwargs=self._kwargs,
                env_prefix="GREL_LOG_",
                env_load=self._env_load,
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
