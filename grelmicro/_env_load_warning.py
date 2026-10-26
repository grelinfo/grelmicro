"""`EnvLoadDefaultWarning`, served with a warning until its removal in 0.45.0."""

from typing import ClassVar
from warnings import deprecated

from grelmicro.errors import EnvLoadOffWarning, Grelmicro045Warning


@deprecated(
    "`EnvLoadDefaultWarning` is removed in 0.45.0. "
    "Use `EnvLoadOffWarning` instead.",
    category=None,
)
class EnvLoadDefaultWarning(Grelmicro045Warning, EnvLoadOffWarning):
    """A `GREL_*` variable was set while `GREL_ENV_LOAD` was unset.

    No longer emitted: a `GREL_*` variable is read by default. A variable
    left unread by `GREL_ENV_LOAD=0` warns with `EnvLoadOffWarning`.
    Removed in 0.45.0.
    """

    code: ClassVar[str] = "env-load-off"


EnvLoadDefaultWarning.__module__ = "grelmicro.errors"


def env_load_default_warning() -> type[EnvLoadDefaultWarning]:
    """Return `EnvLoadDefaultWarning` and warn that it is removed.

    Call it from a module `__getattr__`, so the warning reports the line
    that imported or read the name.
    """
    from grelmicro._deprecation import ENV_LOAD_DEFAULT_WARNING  # noqa: PLC0415

    ENV_LOAD_DEFAULT_WARNING.warn(stacklevel=3)
    return EnvLoadDefaultWarning
