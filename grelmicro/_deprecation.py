"""Pending behavior changes, renames and removals, each warned one release ahead.

Declare each one here as a module constant, add it to `PENDING`, and call
its `warn()` where the old behavior or the old name is reached. A test
fails once the changelog reaches the release that should switch the
default or remove the old name.
"""

import warnings
from dataclasses import dataclass, replace

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


def _mode_enum(old: str, example: str, alias: str) -> Removal:
    """Return the removal of the mode enum `old` in favor of a string."""
    return Removal(
        old=old,
        use=f'`"{example}"` and the `{alias}` type',
        removed_in="0.44.0",
    )


MODE_ENUMS: dict[str, Removal] = {
    removal.old: removal
    for removal in (
        _mode_enum("MetricsExporterType", "prometheus", "MetricsExporter"),
        _mode_enum("TraceExporterType", "otlp-http", "TraceExporter"),
        _mode_enum("TraceProcessorType", "batch", "TraceProcessor"),
        _mode_enum("TraceSamplerType", "parentbased_always_on", "TraceSampler"),
        _mode_enum("LogLevelType", "INFO", "LogLevel"),
        _mode_enum("LogFormatType", "JSON", "LogFormat"),
        _mode_enum("LogBackendType", "loguru", "LogBackend"),
        _mode_enum("LogSerializerType", "orjson", "LogSerializer"),
    )
}
"""The mode enums replaced by string literals, by name."""


ENVIRONMENT_CASE = FutureChange(
    change="A tier in another case, such as `GREL_ENVIRONMENT=Production`, "
    "reads as that tier",
    switch_in="0.44.0",
    keep="GREL_ENVIRONMENT=",
    adopt="GREL_ENVIRONMENT=production",
)
"""`GREL_ENVIRONMENT` matches the tier in any case."""


def environment_case(value: str, tier: str) -> FutureChange:
    """Return `ENVIRONMENT_CASE` worded for one `GREL_ENVIRONMENT` value.

    `value` is the spelling given, and `tier` the tier it names in another
    case.
    """
    return replace(
        ENVIRONMENT_CASE,
        change=f"`GREL_ENVIRONMENT={value}` reads as `{tier}`",
        adopt=f"GREL_ENVIRONMENT={tier}",
    )


PENDING: tuple[FutureChange | Rename | Removal, ...] = (
    *MODE_ENUMS.values(),
    ENVIRONMENT_CASE,
)
"""Every behavior change, rename and removal that currently warns."""
