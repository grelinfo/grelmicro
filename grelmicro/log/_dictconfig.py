"""Logging configuration an application server can consume."""

from typing import Annotated, Any

from typing_extensions import Doc

from grelmicro.log._shared import load_settings
from grelmicro.log.config import LogConfig

_ACCESS_LOGGER = "uvicorn.access"
"""Uvicorn's access logger, the one that needs a formatter of its own.

Uvicorn logs a request as a template and five positional arguments, so the
record carries the method, the path and the status as arguments rather than
as fields. Gunicorn, Hypercorn and Granian render the line before they log
it, and those read like any other record.
"""

_SERVER_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "gunicorn.error",
    "gunicorn.access",
    "hypercorn.error",
    "hypercorn.access",
    "_granian",
    "granian.access",
)
"""Loggers an application server writes to, and hands to the root logger.

Every name is set whether or not that server is installed. A logger that
nothing writes to costs one object, and naming them all is what lets one
document serve whichever server started the process.
"""


def _build(
    config: LogConfig | None, *, env_load: bool | None = None
) -> dict[str, Any]:
    """Assemble the document against `config`, or against the environment."""
    settings = load_settings(config, env_load=env_load).settings.model_dump(
        mode="json"
    )
    # Each entry carries its own copy, so editing one by hand does not
    # reach into the other three.
    default: dict[str, Any] = {
        "()": "grelmicro.log.formatter",
        "config": dict(settings),
    }
    access: dict[str, Any] = {
        "()": "grelmicro.log.uvicorn.UvicornAccessFormatter",
        "config": dict(settings),
    }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        # Uvicorn writes `use_colors` into the formatter named `default` and
        # the one named `access` when it is started with `--use-colors` or
        # `--no-use-colors`, and raises when either name is missing.
        "formatters": {"default": default, "access": access},
        "handlers": {
            "default": {
                "()": "grelmicro.log.handler",
                "config": dict(settings),
                "formatter": "default",
            },
            "access": {
                "()": "grelmicro.log.handler",
                "config": dict(settings),
                "formatter": "access",
            },
        },
        "loggers": {
            **{
                name: {"handlers": [], "propagate": True}
                for name in _SERVER_LOGGERS
            },
            _ACCESS_LOGGER: {"handlers": ["access"], "propagate": False},
        },
        "root": {"handlers": ["default"], "level": settings["level"]},
    }


def dict_config(
    *,
    env_load: Annotated[
        bool | None,
        Doc(
            "Whether to read `GREL_LOG_*` environment variables. "
            "When None (default), follow `GREL_ENV_LOAD`. "
            "Pass True or False to override."
        ),
    ] = None,
) -> dict[str, Any]:
    """Return a logging configuration that renders records in this format.

    Hand it to the application server and the process reads in one format
    from its first record, including the records written before the
    application module is imported:

    ```python
    uvicorn.run(app, log_config=dict_config())
    ```

    The same document is what Gunicorn takes as `logconfig_dict`,
    Hypercorn as `logconfig_dict`, and Granian as `log_dictconfig`. Every
    logger those servers write to is given to the root logger, so a server
    line and an application line render the same way.

    It is a plain `logging.config.dictConfig` document, so it is also what
    goes in the file `uvicorn --log-config` reads:

    ```python
    Path("logging.json").write_text(json.dumps(dict_config()))
    ```

    Fields resolve from `GREL_LOG_*` when the document is built, and the
    document carries them. It is a snapshot, not a template, so build it
    where the process starts rather than where an image does. Reading the
    environment is opt-in, the same as everywhere else: set
    `GREL_ENV_LOAD=1`, or pass `env_load=True` from a process that cannot
    set it. To render against settings that never reach the environment,
    use [`dict_config_with()`][grelmicro.log.dict_config_with].

    A document applied on its own is behind the queue `queue_enabled` asks
    for, because the handler starts the writer when none is running.

    An application that also writes through loguru or structlog calls
    [`configure()`][grelmicro.log.configure] as well, which adds the
    backend. The root logger ends up configured twice and reads the same
    either way, because each pass replaces the handler rather than adding
    one.

    Returns:
        A `dictConfig` document. JSON-serializable, so it can be written to
        a file, and a fresh copy on every call, so a server that writes
        into it changes nothing else.

    Raises:
        DependencyNotFoundError: If orjson or OpenTelemetry is enabled but not installed.
        SettingsValidationError: If configuration is invalid.
    """
    return _build(None, env_load=env_load)


def dict_config_with(
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
) -> dict[str, Any]:
    """Return a logging configuration built from a pre-built `LogConfig`.

    The same document [`dict_config()`][grelmicro.log.dict_config] returns,
    built from settings given here rather than read from the environment.

    Returns:
        A `dictConfig` document, JSON-serializable like the one
        [`dict_config()`][grelmicro.log.dict_config] returns.

    Raises:
        DependencyNotFoundError: If orjson or OpenTelemetry is enabled but not installed.
        SettingsValidationError: If configuration is invalid.
    """
    return _build(config)
