"""Timezone name validation and resolution.

One acceptance set for every surface that takes a timezone, so the same
string is accepted or rejected identically whether it arrives as a
keyword argument, an environment variable, or a config object.

Names are validated against what `zoneinfo` can actually load, rather
than against a snapshot of the timezone database taken at import. A
name that validates here always resolves in `resolve_timezone`.
"""

from __future__ import annotations

from datetime import UTC, tzinfo
from difflib import get_close_matches
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC_NAME = "UTC"
"""The default timezone, resolvable without a timezone database."""

SHARED_TIMEZONE_ENV = {"timezone": "GREL_TIMEZONE"}
"""Maps the `timezone` field to the app-wide variable that fills it.

Passed as `shared_env` by every component that takes a timezone, so one
variable says what wall clock the service runs on.
"""

_TZDATA_HINT = (
    "no timezone database was found, install the tzdata package or add "
    "the zoneinfo files to the image"
)


@cache
def _known_timezones() -> dict[str, str]:
    """Map each upper-cased timezone name to its correctly cased form.

    Built once on first use rather than at import, so a process without a
    timezone database still imports grelmicro and still runs on the
    default `UTC`.
    """
    return {name.upper(): name for name in available_timezones()}


def normalize_timezone_name(value: str) -> str:
    """Return ``value`` in the casing the timezone database uses.

    Accepts any casing, so ``europe/zurich`` resolves the same way on a
    case-sensitive filesystem as it does on a case-insensitive one.

    Raises:
        ValueError: If no timezone of that name can be loaded.
    """
    name = value.strip()
    if not name:
        msg = "timezone name is empty"
        raise ValueError(msg)
    if name.upper() == UTC_NAME:
        return UTC_NAME
    known = _known_timezones()
    if known:
        correct = known.get(name.upper())
        if correct is None:
            msg = f"unknown timezone name{_did_you_mean(name)}"
            raise ValueError(msg)
        return correct
    # No timezone database, so the name cannot be checked against a list.
    # Loading it is the only remaining test, and it also reports whether
    # the database is missing entirely.
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        msg = f"unknown timezone name, {_TZDATA_HINT}"
        raise ValueError(msg) from None
    return name


_MIN_HINT_LENGTH = 5
"""Shortest name that earns a hint when it carries no region.

`PST`, `CEST` and the other abbreviations are the common mistake, and
each sits close enough to `MST` or `EST` for a match. A hint naming the
wrong continent is worse than none, and the rejected name is not echoed,
so the hint is the only thing the operator reads.
"""

_HINT_CUTOFF = 0.75
"""How close a candidate must be before it is offered."""


@cache
def _hint_candidates() -> dict[str, str]:
    """Map every full name and city segment to its correctly cased zone.

    The city segment is matched too, so `Zurich` reaches `Europe/Zurich`,
    which the full name alone scores too far apart to reach.
    """
    candidates: dict[str, str] = {}
    for upper, correct in sorted(_known_timezones().items()):
        candidates.setdefault(upper, correct)
        candidates.setdefault(upper.rpartition("/")[2], correct)
    return candidates


def _did_you_mean(name: str) -> str:
    """Return a `, did you mean 'X'` hint, or an empty string.

    The rejected value is never repeated: it is arbitrary operator input,
    and a variable that should hold a zone can hold anything, including a
    credential put there by a swapped variable. A *member* of the timezone
    database carries the same diagnostic and can never be a secret.
    """
    upper = name.upper()
    if "/" not in upper and len(upper) < _MIN_HINT_LENGTH:
        return ""
    candidates = _hint_candidates()
    close = get_close_matches(upper, candidates, n=1, cutoff=_HINT_CUTOFF)
    if not close:
        return ""
    return f", did you mean {candidates[close[0]]!r}"


def resolve_timezone(name: str) -> tzinfo:
    """Return the `tzinfo` for a timezone name.

    Accepts the same names as `normalize_timezone_name`, in any casing,
    so a name that validates anywhere resolves here. ``UTC`` resolves
    without touching the timezone database, so the default works on an
    image that carries no timezone files.

    Raises:
        ValueError: If no timezone of that name can be loaded.
    """
    normalized = normalize_timezone_name(name)
    if normalized == UTC_NAME:
        return UTC
    return ZoneInfo(normalized)
