"""Shared type aliases used across grelmicro modules.

Lightweight by design. Modules that depend on a small primitive type
(e.g. a `Literal` or a narrow `TypeAlias`) import from here instead
of from a heavyweight module. Keeps the import graph clean.
"""

from typing import Literal, TypeAlias

LogLevel: TypeAlias = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
"""Standard logging level names, matching `logging.getLevelName` output."""
