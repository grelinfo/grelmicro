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

from grelmicro._deprecation import FutureChange, Removal, Rename
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

    `default` leaves the argument at its default, or uses the old name.
    `explicit` passes the argument, or uses the new name.
    """
    with pytest.warns(GrelmicroFutureWarning, match=re.escape(entry.message)):
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
    entries: Iterable[FutureChange | Rename | Removal], version: str
) -> list[FutureChange | Rename | Removal]:
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
