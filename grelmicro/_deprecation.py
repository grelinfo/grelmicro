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
        "left out or replace another with no error. A table name takes only "
        "ASCII letters, digits and underscores, one byte each. "
        f"Pass a `{argument}` of {longest} characters or fewer.",
    )


def _table_names_remedy(provider: str) -> str:
    """Return the advice to pick the table names on `provider`."""
    return (
        f'Pass `table_names="legacy"` to `{provider}` to keep the current '
        'table names, or `table_names="prefixed"` to adopt the new ones now. '
        "A `table_name=` passed to the store wins."
    )


DEFAULT_TABLE_NAME = FutureChange(
    change="`PostgresLockAdapter(table_name=)` defaults to `grelmicro_locks`",
    switch_in="0.46.0",
    keep='table_names="legacy"',
    adopt='table_names="prefixed"',
    remedy=_table_names_remedy("PostgresProvider"),
)
"""A default table name takes the `grelmicro_` prefix."""


def default_table_name(*, adapter: str, new: str) -> FutureChange:
    """Return `DEFAULT_TABLE_NAME` worded for `adapter`.

    `new` is its default table name from 0.46.0. The advice names the
    provider that builds `adapter`.
    """
    provider = (
        "PostgresProvider"
        if adapter.startswith("Postgres")
        else "SQLiteProvider"
    )
    return replace(
        DEFAULT_TABLE_NAME,
        change=f"`{adapter}(table_name=)` defaults to `{new}`",
        remedy=_table_names_remedy(provider),
    )


INHERITED_OUTBOX_TOPIC = FutureChange(
    change="A payload model that inherits `__outbox_topic__` takes its "
    "class name as its outbox topic",
    switch_in="0.46.0",
    keep=None,
    adopt="__outbox_topic__",
    remedy="Set `__outbox_topic__` on the model itself to pick its topic.",
)
"""A payload model takes its class name, not a parent's `__outbox_topic__`."""


def inherited_outbox_topic(
    *, model: str, parent: str, topic: str
) -> FutureChange:
    """Return `INHERITED_OUTBOX_TOPIC` worded for `model`.

    `model` inherits `topic` from `parent`, each named as the message names
    it.
    """
    return replace(
        INHERITED_OUTBOX_TOPIC,
        change=f"`{model}` takes its class name as its outbox topic",
        remedy=f"Until then it inherits `{topic}` from `{parent}`. "
        f'Set `__outbox_topic__ = "{topic}"` on `{model}` to keep the '
        f'current topic, or `__outbox_topic__ = "{model}"` to take its '
        "class name now.",
    )


GENERIC_OUTBOX_TOPIC = FutureChange(
    change="A parametrized generic payload model takes the outbox topic of "
    "its generic origin",
    switch_in="0.46.0",
    keep=None,
    adopt="__outbox_topic__",
    remedy="Set `__outbox_topic__` on the generic model to pick its topic.",
)
"""A parametrized generic payload model takes its origin's name as its topic."""


def generic_outbox_topic(*, model: str, origin: str) -> FutureChange:
    """Return `GENERIC_OUTBOX_TOPIC` worded for `model`.

    `model` is a parametrization of `origin`, such as `Event[int]` of
    `Event`, each named as the message names it.
    """
    return replace(
        GENERIC_OUTBOX_TOPIC,
        change=f"`{model}` and every other parametrization of `{origin}` "
        f"take the outbox topic `{origin}`",
        remedy=f"Until then the topic of `{model}` is `{model}`. "
        f"Set `__outbox_topic__` on `{origin}` to pick one topic for all of "
        f'them now, or `__outbox_topic__ = "{model}"` on a subclass of '
        f"`{model}` to keep this topic.",
    )


SPLIT_TABLES = FutureChange(
    change="`PostgresLockAdapter` opens `public.locks` while "
    "`public.grelmicro_locks` exists, the table it defaults to",
    switch_in="0.46.0",
    keep=None,
    adopt="table_names=",
    remedy="Replicas may be split across the two tables, each seeing only "
    "its own state. Give every replica the same `table_names=` on its "
    "provider.",
)
"""An adapter opens its old default table while the new one exists."""


def split_tables(*, adapter: str, old: str, new: str) -> FutureChange:
    """Return `SPLIT_TABLES` worded for `adapter`.

    `old` is the table it opens, and `new` the one it defaults to from 0.46.0,
    each as the message names it.
    """
    return replace(
        SPLIT_TABLES,
        change=f"`{adapter}` opens `{old}` while `{new}` exists, "
        "the table it defaults to",
    )


DEFAULT_FINGERPRINT_BODY = FutureChange(
    change="`IdempotentRequests(fingerprint_body=)` defaults to `True`",
    switch_in="0.46.0",
    keep="fingerprint_body=False",
    adopt="fingerprint_body=True",
)
"""A key reused with a different body is refused by default."""


def default_fingerprint_body(owner: str) -> FutureChange:
    """Return `DEFAULT_FINGERPRINT_BODY` worded for `owner`.

    `owner` is the class that took `fingerprint_body`, such as
    `IdempotencyMiddleware`.
    """
    return replace(
        DEFAULT_FINGERPRINT_BODY,
        change=f"`{owner}(fingerprint_body=)` defaults to `True`",
    )


PENDING: tuple[FutureChange | Rename | Removal | Moved, ...] = (
    UNKNOWN_REQUIRES,
    LONG_TABLE_NAME,
    DEFAULT_TABLE_NAME,
    SPLIT_TABLES,
    INHERITED_OUTBOX_TOPIC,
    GENERIC_OUTBOX_TOPIC,
    DEFAULT_FINGERPRINT_BODY,
)
"""Every behavior change, rename and removal that currently warns."""
