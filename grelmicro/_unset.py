"""Sentinels for a keyword the caller did not pass."""

from typing import Final


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


def warn_default_table_name(table_name: str, *, adapter: str) -> None:
    """Warn while `table_name` is left at a default that changes in 0.46.0.

    `adapter` is the class that took `table_name`. The warning reports the
    first line outside grelmicro. A name passed in warns nothing.
    """
    if not isinstance(table_name, DefaultTableName):
        return
    from grelmicro._deprecation import default_table_name  # noqa: PLC0415

    default_table_name(adapter=adapter, new=table_name.becomes).warn(
        outside=True
    )
