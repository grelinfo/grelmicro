"""Pending behavior changes, renames and removals, each warned one release ahead.

Declare each one here as a module constant, add it to `PENDING`, and call
its `warn()` where the old behavior or the old name is reached. Each one warns
with the `GrelmicroFutureWarning` subclass of the release that switches
it. A test fails once the changelog reaches the release that should switch
the default or remove the old name.
"""

import warnings
from dataclasses import dataclass, replace
from typing import Literal

from grelmicro.errors import Grelmicro045Warning, GrelmicroFutureWarning

RELEASE_WARNINGS: dict[str, type[GrelmicroFutureWarning]] = {
    category.switch_in: category for category in (Grelmicro045Warning,)
}
"""The `GrelmicroFutureWarning` subclass of each switching release, by release."""


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

    @property
    def category(self) -> type[GrelmicroFutureWarning]:
        """The warning category `warn` emits."""
        return self.narrow or RELEASE_WARNINGS[self.switch_in]

    @property
    def message(self) -> str:
        """The warning message."""
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

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `category` warning with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
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


ENV_LOAD_DEFAULT_WARNING = Removal(
    old="EnvLoadDefaultWarning",
    use="`EnvLoadOffWarning`",
    removed_in="0.45.0",
)
"""`EnvLoadDefaultWarning`, no longer emitted since `GREL_*` is read by default."""


FORMAT_CASE = FutureChange(
    change='`LogConfig.format` reads back in lowercase, such as `"json"` '
    'rather than `"JSON"`',
    switch_in="0.45.0",
    keep=None,
    adopt='config.format.lower() == "json"',
    verb="Compare",
)
"""A built-in `LogConfig.format` reads back in lowercase."""


def format_case(name: str) -> FutureChange:
    """Return `FORMAT_CASE` worded for the built-in format `name`.

    `name` is the format in lowercase, such as `json`.
    """
    return replace(
        FORMAT_CASE,
        change=f'`LogConfig.format` reads back as `"{name}"` '
        f'rather than `"{name.upper()}"`',
        adopt=f'config.format.lower() == "{name}"',
    )


PROVIDER_CHECKS_NONE = Removal(
    old="provider_checks=None",
    use='`provider_checks="report"` or no `provider_checks=`',
    removed_in="0.45.0",
)
"""`HealthChecks(provider_checks=None)`, which reads as `"report"`."""


GATE_BACKEND_STRICT = FutureChange(
    change="`gate-backend` fails `grelmicro check` in `staging` and "
    "`production`",
    switch_in="0.45.0",
    keep=None,
    adopt="uses=[Coordination(redis)]",
)
"""A gated task that resolves no backend fails the check in a strict tier."""


PENDING: tuple[FutureChange | Rename | Removal, ...] = (
    GATE_BACKEND_STRICT,
    ENV_LOAD_DEFAULT_WARNING,
    FORMAT_CASE,
    PROVIDER_CHECKS_NONE,
)
"""Every behavior change, rename and removal that currently warns."""
