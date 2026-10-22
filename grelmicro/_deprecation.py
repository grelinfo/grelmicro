"""Pending behavior changes and renames, each warned one release ahead.

Declare each one here as a module constant, add it to `PENDING`, and call
its `warn()` where the old behavior or the old name is reached. A test
fails once the changelog reaches the release that should switch the
default or remove the old name.
"""

import warnings
from dataclasses import dataclass

from grelmicro.errors import GrelmicroFutureWarning


@dataclass(frozen=True, slots=True, kw_only=True)
class FutureChange:
    """A default that changes in `switch_in`.

    Warn only while the argument is left at its default.
    """

    change: str
    """What changes, as a sentence with no period.

    Such as "`Cron(gate=)` defaults to `leader`".
    """

    switch_in: str
    """The release that switches the default, as `MAJOR.MINOR.PATCH`."""

    keep: str
    """The argument that keeps the current behavior, such as `gate=None`."""

    adopt: str
    """The argument that adopts the new behavior now."""

    @property
    def message(self) -> str:
        """The warning message."""
        return (
            f"{self.change} from {self.switch_in}. "
            f"Pass `{self.keep}` to keep the current behavior, "
            f"or `{self.adopt}` to adopt the new one now."
        )

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `GrelmicroFutureWarning` with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(
            self.message, GrelmicroFutureWarning, stacklevel=stacklevel + 1
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Rename:
    """A public name replaced by `new`, with `old` removed in `removed_in`."""

    old: str
    """The name being retired."""

    new: str
    """The name that replaces it."""

    removed_in: str
    """The release that removes `old`, as `MAJOR.MINOR.PATCH`."""

    @property
    def message(self) -> str:
        """The warning message."""
        return (
            f"`{self.old}` is renamed to `{self.new}`. "
            f"`{self.old}` is removed in {self.removed_in}."
        )

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `GrelmicroFutureWarning` with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(
            self.message, GrelmicroFutureWarning, stacklevel=stacklevel + 1
        )


PENDING: tuple[FutureChange | Rename, ...] = ()
"""Every behavior change and rename that currently warns."""
