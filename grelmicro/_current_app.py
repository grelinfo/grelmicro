"""The context variable that holds the active `Grelmicro` app."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grelmicro._app import Grelmicro

_current_micro: ContextVar[Grelmicro] = ContextVar("grelmicro_current_app")
"""The app open in this context, set by `async with micro:`."""
