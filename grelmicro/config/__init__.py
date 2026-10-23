"""Externalized configuration: reconfigure live components from a source.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.config._external import ExternalConfig
    from grelmicro.config._protocol import ConfigBackend
    from grelmicro.config.file import FileConfigAdapter

__all__ = [
    "ConfigBackend",
    "ExternalConfig",
    "FileConfigAdapter",
]

_LAZY: dict[str, str] = {
    "ConfigBackend": "grelmicro.config._protocol",
    "ExternalConfig": "grelmicro.config._external",
    "FileConfigAdapter": "grelmicro.config.file",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
