"""Pending behavior changes, renames and removals, each warned one release ahead.

Declare each one here as a module constant, add it to `PENDING`, and call
its `warn()` where the old behavior or the old name is reached. A test
fails once the changelog reaches the release that should switch the
default or remove the old name.
"""

import warnings
from dataclasses import dataclass, replace
from typing import Literal

from grelmicro.errors import EnvLoadDefaultWarning, GrelmicroFutureWarning


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

    verb: Literal["Pass", "Set"] = "Pass"
    """How the message asks for `keep` and `adopt`.

    "Pass" for an argument and "Set" for an environment variable.
    """

    category: type[GrelmicroFutureWarning] = GrelmicroFutureWarning
    """The warning category `warn` emits."""

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

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `GrelmicroFutureWarning` with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(
            self.message, GrelmicroFutureWarning, stacklevel=stacklevel + 1
        )


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

    def warn(self, stacklevel: int = 2) -> None:
        """Emit a `GrelmicroFutureWarning` with `message`.

        `stacklevel` counts from the function that calls `warn`, so the
        default reports the line that called that function.
        """
        warnings.warn(
            self.message, GrelmicroFutureWarning, stacklevel=stacklevel + 1
        )


ENVIRONMENT_CASE = FutureChange(
    change="A tier in another case, such as `GREL_ENVIRONMENT=Production`, "
    "reads as undeclared today and as that tier",
    switch_in="0.44.0",
    keep=None,
    adopt="GREL_ENVIRONMENT=production",
    verb="Set",
)
"""`GREL_ENVIRONMENT` matches the tier in any case."""


def environment_case(value: str, tier: str) -> FutureChange:
    """Return `ENVIRONMENT_CASE` worded for one `GREL_ENVIRONMENT` value.

    `value` is the spelling given, and `tier` the tier it names in another
    case.
    """
    return replace(
        ENVIRONMENT_CASE,
        change=f"`GREL_ENVIRONMENT={value}` reads as undeclared today "
        f"and as `{tier}`",
        adopt=f"GREL_ENVIRONMENT={tier}",
    )


ENV_LOAD_DEFAULT = FutureChange(
    change="A set `GREL_*` variable is read by default",
    switch_in="0.44.0",
    keep="GREL_ENV_LOAD=0",
    adopt="GREL_ENV_LOAD=1",
    verb="Set",
    category=EnvLoadDefaultWarning,
)
"""Every `GREL_*` field is read unless `GREL_ENV_LOAD=0` or `env_load=False`."""


def unread_env(name: str) -> FutureChange:
    """Return `ENV_LOAD_DEFAULT` worded for one unread variable `name`.

    Names the variable, never its value.
    """
    return replace(
        ENV_LOAD_DEFAULT,
        change=f"`{name}` is set but not read today, and is read by default",
    )


AUTO_HEALTH: dict[bool, Rename] = {
    True: Rename(
        old="auto_health=True",
        new='provider_checks="require"',
        removed_in="0.44.0",
    ),
    False: Rename(
        old="auto_health=False",
        new='provider_checks="off"',
        removed_in="0.44.0",
    ),
}
"""`HealthChecks(auto_health=)` replaced by `provider_checks=`, by the value passed."""


PROVIDERS_DEFAULT = FutureChange(
    change="`HealthChecks()` reports each Provider in `/healthz` by default",
    switch_in="0.44.0",
    keep='provider_checks="off"',
    adopt='provider_checks="report"',
)
"""`HealthChecks` registers a non-critical check per Provider by default."""


PENDING: tuple[FutureChange | Rename | Removal, ...] = (
    ENVIRONMENT_CASE,
    ENV_LOAD_DEFAULT,
    *AUTO_HEALTH.values(),
    PROVIDERS_DEFAULT,
)
"""Every behavior change, rename and removal that currently warns."""
