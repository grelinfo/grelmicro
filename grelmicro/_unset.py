"""Sentinel for a keyword the caller did not pass."""

from typing import Final


class Unset:
    """Stands for a keyword the caller left out, where `None` is a value."""

    def __repr__(self) -> str:
        """Return the name it is published under."""
        return "UNSET"


UNSET: Final = Unset()
"""The one instance of `Unset`."""
