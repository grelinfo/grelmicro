"""Externalized configuration: reconfigure live components from a source."""

from grelmicro.config._external import ExternalConfig
from grelmicro.config.abc import ConfigBackend
from grelmicro.config.file import FileConfigAdapter

__all__ = [
    "ConfigBackend",
    "ExternalConfig",
    "FileConfigAdapter",
]
