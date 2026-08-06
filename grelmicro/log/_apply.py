"""Backend dispatcher for logging configuration."""

from grelmicro._config import flush_ignored_env_reports
from grelmicro.log.config import LogBackendType, LogConfig


def apply(config: LogConfig) -> None:
    """Dispatch to the selected backend with the resolved config.

    Flushes the queued ignored-variable reports last, so a `GREL_*`
    variable set without `GREL_ENV_LOAD` is named on the `grelmicro`
    logger once the handlers are installed.
    """
    if config.backend == LogBackendType.STRUCTLOG:
        from grelmicro.log._structlog import (  # noqa: PLC0415
            configure as _configure,
        )
    elif config.backend == LogBackendType.STDLIB:
        from grelmicro.log._stdlib import (  # noqa: PLC0415
            configure as _configure,
        )
    else:
        from grelmicro.log._loguru import (  # noqa: PLC0415
            configure as _configure,
        )

    _configure(config)

    if config.uvicorn_enabled:
        from grelmicro.log.uvicorn import (  # noqa: PLC0415
            apply as _apply_uvicorn,
        )

        _apply_uvicorn(config)

    flush_ignored_env_reports()
