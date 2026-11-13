"""Shared helpers for tests of a pending behavior change, rename or removal.

Each `FutureChange` gets one test that calls it both ways:

```python
from tests._future import assert_future_warning

def test_cron_gate_default_warns() -> None:
    assert_future_warning(
        CRON_GATE,
        default=lambda: Cron("* * * * *"),
        explicit=lambda: Cron("* * * * *", gate=None),
    )
```
"""

import re
import warnings
from collections.abc import Callable, Iterable

import pytest

from grelmicro._deprecation import FutureChange, Moved, Removal, Rename
from grelmicro.errors import GrelmicroFutureWarning
from tools import changelog

_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def assert_future_warning(
    entry: FutureChange | Rename | Removal,
    *,
    default: Callable[[], object],
    explicit: Callable[[], object],
) -> None:
    """Assert `default` warns with the message of `entry` and `explicit` warns nothing.

    `default` leaves the argument at its default, or uses the old name, and
    warns under the category of `entry`. `explicit` passes the argument, or
    uses the new name.
    """
    with pytest.warns(entry.category, match=re.escape(entry.message)):
        default()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        explicit()
    leaked = [
        str(warning.message)
        for warning in caught
        if issubclass(warning.category, GrelmicroFutureWarning)
    ]
    assert not leaked, leaked


def _release(version: str) -> tuple[int, int, int]:
    """Return `version` as a comparable `(major, minor, patch)`."""
    match = _RELEASE.match(version)
    assert match, f"{version!r} is not a MAJOR.MINOR.PATCH release"
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def latest_release(text: str) -> str:
    """Return the newest dated version in the changelog `text`.

    `just release` dates the `Unreleased` section before the preflight runs
    the tests, so on a release this is the version about to be tagged.
    """
    for version, (date, _) in changelog.sections(text).items():
        if date is not None:
            return version
    msg = "the changelog has no dated release"
    raise AssertionError(msg)


def stale(
    entries: Iterable[FutureChange | Rename | Removal | Moved], version: str
) -> list[FutureChange | Rename | Removal | Moved]:
    """Return the entries that `version` should already have switched or removed."""
    current = _release(version)
    return [
        entry
        for entry in entries
        if _release(
            entry.switch_in
            if isinstance(entry, FutureChange)
            else entry.removed_in
        )
        <= current
    ]


DEFAULT_TABLE_NAMES_FILTER = (
    r"ignore:`\w+\(table_name=\)` defaults to `grelmicro_\w+` from 0\.46\.0"
    ":grelmicro.errors.Grelmicro046Warning"
)
"""The warning filter that quiets the 0.46.0 default table name warning."""

DEFAULT_TABLE_NAMES = pytest.mark.filterwarnings(DEFAULT_TABLE_NAMES_FILTER)
"""Quiet the 0.46.0 default table name warning in one test.

For a test of a bare provider, or a provider factory called with no
arguments, which builds each adapter with its default table name, so the
test has no `table_name=` to pass. Every other warning still fails the test.
Remove it with the switch in 0.46.0.
"""

DEFAULT_TABLE_NAME_ADAPTERS = (
    "PostgresLockAdapter",
    "PostgresScheduleAdapter",
    "PostgresRateLimiterAdapter",
    "PostgresCircuitBreakerAdapter",
    "SQLiteLockAdapter",
    "SQLiteReadWriteLockAdapter",
    "SQLiteScheduleAdapter",
    "SQLiteRateLimiterAdapter",
    "SQLiteCircuitBreakerAdapter",
)
"""Every adapter whose default table name changes in 0.46.0."""


def default_table_names_options() -> list[str]:
    """Return `pytest -W` options that quiet the 0.46.0 default table name warning.

    A `-W` option matches its message literally, so there is one per adapter.
    """
    return [
        option
        for adapter in DEFAULT_TABLE_NAME_ADAPTERS
        for option in (
            "-W",
            (
                f"ignore:`{adapter}(table_name=)` defaults to"
                ":grelmicro.errors.Grelmicro046Warning"
            ),
        )
    ]
