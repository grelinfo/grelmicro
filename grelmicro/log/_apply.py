"""Backend dispatcher for logging configuration."""

from grelmicro.log.config import LoggingBackendType, LoggingConfig


def apply(config: LoggingConfig) -> None:
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
