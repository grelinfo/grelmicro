"""Log component for the Grelmicro app object."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._config import (
    hold_ignored_env_reports,
    ignored_env_reports_enabled,
    resolve_config,
)
from grelmicro._timezone import SHARED_TIMEZONE_ENV
from grelmicro.log._apply import apply as _apply
from grelmicro.log.config import (
    LogBackendType,
    LogConfig,
    LogFormatType,
    LogLevelType,
    LogSerializerType,
)

if TYPE_CHECKING:
    from types import TracebackType

    from grelmicro.types import TimeZoneName


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
    sequential `Grelmicro(...)` blocks do not pile handlers up. Every
    backend installs a root handler, so every backend gives the root
    logger back. What each one installs for its own records, loguru's sink
    and structlog's processors, stays configured: those are process-wide
    settings of libraries the app chose, not state this component took.

    The stdlib root logger is a single global. `Log.__aenter__` and
    `Log.__aexit__` serialize on a class-level `threading.Lock` so the
    snapshot/restore sequence cannot interleave across concurrent
    `Grelmicro` lifecycles in the same process. Run one `Log` at a
    time per process.

    Read more in the [Logging](../logging/index.md) docs.
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
            TimeZoneName | None,
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
        self._setup(
            name=name,
            config=None,
            kwargs={
                "backend": backend,
                "level": level,
                "format": format,
                "timezone": timezone,
                "json_serializer": json_serializer,
                "caller_enabled": caller_enabled,
                "otel_enabled": otel_enabled,
            },
            env_load=env_load,
        )

    def _setup(
        self,
        *,
        name: str,
        config: LogConfig | None,
        kwargs: dict[str, Any],
        env_load: bool | None,
    ) -> None:
        """Wire the deferred configuration onto the instance."""
        self._name = name
        self._explicit_config = config
        self._kwargs = kwargs
        self._env_load = env_load
        self._resolved: LogConfig | None = None
        self._snapshot_handlers: list[logging.Handler] | None = None
        self._snapshot_level: int | None = None
        self._snapshot_reports = False

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
        instance = cls.__new__(cls)
        instance._setup(  # noqa: SLF001
            name=name, config=config, kwargs={}, env_load=None
        )
        return instance

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
            self._snapshot_reports = ignored_env_reports_enabled()
            self._resolved = resolve_config(
                LogConfig,
                explicit=self._explicit_config,
                kwargs=self._kwargs,
                env_prefix="GREL_LOG_",
                shared_env=SHARED_TIMEZONE_ENV,
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
        """Restore the snapshotted stdlib root handlers and level.

        The ignored-variable reports are restored the same way. They queue
        again when nothing was configured before this lifecycle, so a report
        made after the restore waits for the next one instead of reaching a
        root logger with nothing installed. When an earlier `configure()`
        left logging in place, its handlers come back and reporting with them.
        """
        with self._lifecycle_lock:
            root = logging.getLogger()
            for handler in list(root.handlers):
                root.removeHandler(handler)
            if self._snapshot_handlers is not None:  # pragma: no branch
                for handler in self._snapshot_handlers:
                    root.addHandler(handler)
            if self._snapshot_level is not None:  # pragma: no branch
                root.setLevel(self._snapshot_level)
            if not self._snapshot_reports:
                hold_ignored_env_reports()
            self._snapshot_handlers = None
            self._snapshot_level = None
        return None
