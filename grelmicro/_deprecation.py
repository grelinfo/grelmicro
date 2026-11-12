"""Pending behavior changes, renames and removals, each warned one release ahead.

Declare each one here as a module constant, add it to `PENDING`, and call
its `warn()` where the old behavior or the old name is reached, or the
`load()` of a `Moved` entry from the module `__getattr__`. Each one warns
with the `GrelmicroFutureWarning` subclass of the release that switches
it. A test fails once the changelog reaches the release that should switch
the default or remove the old name.
"""

import importlib
import os
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from grelmicro.errors import (
    Grelmicro045Warning,
    Grelmicro046Warning,
    GrelmicroFutureWarning,
)

_PACKAGE_DIR = f"{Path(__file__).parent}{os.sep}"
"""Frames under this directory are skipped by `warn(outside=True)`."""

RELEASE_WARNINGS: dict[str, type[GrelmicroFutureWarning]] = {
    category.switch_in: category
    for category in (Grelmicro045Warning, Grelmicro046Warning)
}
"""The `GrelmicroFutureWarning` subclass of each switching release, by release."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FutureChange:
    """A behavior that changes in `switch_in`.

    A changed default warns only while the argument is left at its default.
    A changed check result, or a value that becomes refused, warns each time
    the check or the value is reached.
    """

    change: str
    """What changes, as a sentence with no period.

    Such as "`Cron(gate=)` defaults to `leader`".
    """

    switch_in: str
    """The release that switches the default, as `MAJOR.MINOR.PATCH`."""

    keep: str | None
    """The setting that keeps the current behavior, such as `gate=None`.

    None when no setting keeps it, so the message offers only `adopt`.
    """

    adopt: str
    """The setting that adopts the new behavior now."""

    verb: Literal["Pass", "Set", "Compare"] = "Pass"
    """How the message asks for `keep` and `adopt`.

    "Pass" for an argument, "Set" for an environment variable, and
    "Compare" for a value read back.
    """

    narrow: type[GrelmicroFutureWarning] | None = None
    """A subclass of the release's category for this change alone.

    None warns with the release's category.
    """

    remedy: str | None = None
    """A sentence that replaces the `keep` and `adopt` advice, with its period.

    For a fix that no single setting names. None words the advice from
    `keep` and `adopt`.
    """

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits."""
        return self.narrow or RELEASE_WARNINGS[self.switch_in]

    @property
    def message(self) -> str:
        """The warning message."""
        if self.remedy is not None:
            return f"{self.change} from {self.switch_in}. {self.remedy}"
        if self.keep is None:
            return (
                f"{self.change} from {self.switch_in}. "
                f"{self.verb} `{self.adopt}` now."
            )
        return (
            f"{self.change} from {self.switch_in}. "
            f"{self.verb} `{self.keep}` to keep the current behavior, "
            f"or `{self.adopt}` to adopt the new one now."
        )

    def warn(self, stacklevel: int = 2, *, outside: bool = False) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function. With `outside`,
        the warning reports the first line outside grelmicro instead, for a
        change reached through several grelmicro frames.
        """
        if outside:
            warnings.warn(
                self.message,
                self.category,
                skip_file_prefixes=(_PACKAGE_DIR,),
            )
            return
        warnings.warn(self.message, self.category, stacklevel=stacklevel + 1)


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

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits, the one of `removed_in`."""
        return RELEASE_WARNINGS[self.removed_in]

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(self.message, self.category, stacklevel=stacklevel + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Removal:
    """A public name removed in `removed_in`, with `use` in its place."""

    old: str
    """The name being retired."""

    use: str
    """What to use instead, as a phrase with no period.

    Such as "`\"prometheus\"` and the `MetricsExporter` type".
    """

    removed_in: str
    """The release that removes `old`, as `MAJOR.MINOR.PATCH`."""

    @property
    def message(self) -> str:
        """The warning message."""
        return (
            f"`{self.old}` is removed in {self.removed_in}. "
            f"Use {self.use} instead."
        )

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits, the one of `removed_in`."""
        return RELEASE_WARNINGS[self.removed_in]

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(self.message, self.category, stacklevel=stacklevel + 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Moved:
    """Names that `module` serves until `removed_in`, each from its home.

    `module` loads each name from its home on access and warns. The names
    stay out of its `__all__` and `dir()`.
    """

    module: str
    """The module the names leave, such as `grelmicro.integrations.fastapi`."""

    homes: Mapping[str, str] = field(repr=False)
    """The module each name loads from, by name.

    A home with a leading underscore in any part is private, and the message
    says so rather than asking for an import from it.
    """

    removed_in: str
    """The release that removes the names, as `MAJOR.MINOR.PATCH`."""

    @property
    def change(self) -> str:
        """What changes, as a phrase with no period."""
        return f"names `{self.module}` served without listing them"

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `load` emits, the one of `removed_in`."""
        return RELEASE_WARNINGS[self.removed_in]

    def message_for(self, name: str) -> str:
        """Return the warning message for `name`."""
        home = self.homes[name]
        moves = f"`{name}` moves out of `{self.module}` in {self.removed_in}."
        if any(part.startswith("_") for part in home.split(".")):
            return f"{moves} It is private and lives in `{home}`."
        return f"{moves} Import it from `{home}` instead."

    def load(self, name: str, stacklevel: int = 2) -> object:
        """Return `name` from its home and emit a `category` warning.

        Raise `AttributeError` for a name `module` never served.
        `stacklevel` counts from the function that calls `load`, so the
        default reports the line that called that function.
        """
        home = self.homes.get(name)
        if home is None:
            msg = f"module {self.module!r} has no attribute {name!r}"
            raise AttributeError(msg)
        value = getattr(importlib.import_module(home), name)
        warnings.warn(
            self.message_for(name), self.category, stacklevel=stacklevel + 1
        )
        return value


UNKNOWN_REQUIRES = FutureChange(
    change="A `requires=` value that names no scope raises "
    "`SettingsValidationError`",
    switch_in="0.46.0",
    keep=None,
    adopt='requires="cluster"',
    remedy='Pass `"process"`, `"host"` or `"cluster"`.',
)
"""A component `requires=` that names no scope, in any case, is refused."""


def unknown_requires(value: object) -> FutureChange:
    """Return `UNKNOWN_REQUIRES` worded for the refused `value`.

    It says what `value` does until the switch: an empty one reads as the
    component's default, and any other turns the backend scope check off.
    """
    until = (
        "Until then it reads as the default."
        if not value
        else "Until then the backend scope check passes over it."
    )
    return replace(
        UNKNOWN_REQUIRES,
        change=f"`requires={value!r}` names no scope, and raises "
        "`SettingsValidationError`",
        remedy=f'{until} Pass `"process"`, `"host"` or `"cluster"`.',
    )


LONG_TABLE_NAME = FutureChange(
    change="A Postgres table name that cuts a name built from it raises "
    "`SettingsValidationError`",
    switch_in="0.46.0",
    keep=None,
    adopt="table_name=",
    remedy="Pass a shorter table name.",
)
"""A Postgres table name that cuts a name built from it is refused."""


def long_table_name(
    value: str, *, adapter: str, argument: str, longest: int
) -> FutureChange:
    """Return `LONG_TABLE_NAME` worded for the refused `value`.

    `adapter` is the call that took `value` as `argument`, and `longest` is
    the longest table name it takes.
    """
    return replace(
        LONG_TABLE_NAME,
        change=f"`{adapter}({argument}={value!r})` is longer than the "
        f"{longest} characters it takes, and raises `SettingsValidationError`",
        adopt=f"{argument}=",
        remedy="Until then Postgres cuts the names the adapter builds from "
        "it, such as its indexes and functions, at 63 bytes, so one can be "
        "left out or replace another with no error. "
        f"Pass a `{argument}` of {longest} characters or fewer.",
    )


PENDING: tuple[FutureChange | Rename | Removal | Moved, ...] = (
    UNKNOWN_REQUIRES,
    LONG_TABLE_NAME,
)
"""Every behavior change, rename and removal that currently warns."""
