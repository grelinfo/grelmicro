"""Backend dispatcher for logging configuration."""

from grelmicro._config import flush_ignored_env_reports
from grelmicro.log._queue import restore as _restore
from grelmicro.log._queue import swap as _swap
from grelmicro.log.config import LogBackendType, LogConfig


def apply(config: LogConfig) -> None:
    """Dispatch to the selected backend with the resolved config.

    The queued writer is swapped in first, because each backend binds the
    stream it writes to while configuring. The writer it replaced is
    stopped last, once nothing points at it any more, so the records it
    was still holding reach the stream rather than being dropped. It is
    stopped once the new one is wired in. A backend that refuses the
    config puts the old writer back instead and stops the unused one, so
    a `configure()` that raises leaves neither a queue no backend writes
    to nor a working queue torn down by a call that failed.

    Flushes the queued ignored-variable reports last, so a `GREL_*`
    variable set without `GREL_ENV_LOAD` is named on the `grelmicro`
    logger once the handlers are installed.

    Stopping the replaced writer waits for it, bounded by the join
    timeout. On a reconfigure from inside a running event loop that wait
    is on the loop, unlike `Log.__aexit__`, which hands it to a thread.
    Reconfiguring under a stalled sink is the one case where it shows.
    """
    previous = _swap(size=config.queue_size if config.queue_enabled else None)
    try:
        _configure_backend(config)
    except BaseException:
        # No backend was bound to the new writer, so put the one that was
        # working back and stop the unused one. A call that raised should
        # leave logging as it found it, not quietly unqueue it.
        unused = _restore(previous)
        if unused is not None:
            unused.shutdown()
        raise
    else:
        if previous is not None:
            previous.shutdown()

    flush_ignored_env_reports()


def _configure_backend(config: LogConfig) -> None:
    """Configure the selected backend, then uvicorn's own loggers."""
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
