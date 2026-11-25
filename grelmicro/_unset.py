"""Sentinels for a keyword the caller did not pass."""

import os
import sys
from pathlib import Path
from types import FrameType
from typing import Any, Final, NamedTuple


class Unset:
    """Stands for a keyword the caller left out, where `None` is a value."""

    def __repr__(self) -> str:
        """Return the name it is published under."""
        return "UNSET"


UNSET: Final = Unset()
"""The one instance of `Unset`."""


_NEW_DEFAULT_TABLE_NAMES: Final = {
    "locks": "grelmicro_locks",
    "rwlocks": "grelmicro_rwlocks",
    "schedules": "grelmicro_schedules",
}
"""The default table name from 0.46.0, by the default it replaces."""


class DefaultTableName(str):
    """A table name left at its default, which changes in 0.46.0.

    It equals the default, so the adapter uses it as before. Its type tells
    it apart from the same name passed in, which keeps the name it gives.
    """

    __slots__ = ()

    @property
    def becomes(self) -> str:
        """The default table name from 0.46.0."""
        return _NEW_DEFAULT_TABLE_NAMES[self]


_PACKAGE_DIR: Final = f"{Path(__file__).parent}{os.sep}"
"""Frames under this directory are passed over by `default_name_site`."""


class CallSite(NamedTuple):
    """A line of the caller's code, kept to report a later warning there."""

    filename: str
    lineno: int
    module: str
    registry: dict[Any, Any]
    """The once-per-line warning registry of the module at that line."""


def default_name_site(table_name: str) -> CallSite | None:
    """Return the first line outside grelmicro, when `table_name` is a default.

    An adapter calls it while it is built, so the warning it gives when it
    opens reports the line that built it, such as `Grelmicro(uses=[...])`.
    A name passed in returns `None`.
    """
    if not isinstance(table_name, DefaultTableName):
        return None
    frame: FrameType = sys._getframe(1)  # noqa: SLF001
    while frame.f_back is not None and frame.f_code.co_filename.startswith(
        _PACKAGE_DIR
    ):
        frame = frame.f_back
    module_globals = frame.f_globals
    return CallSite(
        frame.f_code.co_filename,
        frame.f_lineno,
        module_globals.get("__name__", "<unknown>"),
        module_globals.setdefault("__warningregistry__", {}),
    )


def warn_default_table_name(
    table_name: str, *, adapter: str, at: CallSite | None
) -> None:
    """Warn while `table_name` is left at a default that changes in 0.46.0.

    `adapter` is the class that took `table_name`. The warning reports the
    line `at`, which `default_name_site` kept when the adapter was built.
    A name passed in warns nothing.
    """
    if not isinstance(table_name, DefaultTableName) or at is None:
        return
    from grelmicro._deprecation import default_table_name  # noqa: PLC0415

    default_table_name(adapter=adapter, new=table_name.becomes).warn_at(at)
