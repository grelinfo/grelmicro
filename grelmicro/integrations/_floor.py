"""The oldest framework versions the integrations run on.

`require` checks one framework against its floor and raises
`DependencyVersionError` naming the floor and the extra that installs it.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING, Final

from grelmicro.errors import DependencyVersionError

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["FASTAPI_FLOOR", "STARLETTE_FLOOR", "require"]

FASTAPI_FLOOR: Final = "0.142.1"
"""The oldest FastAPI the `fastapi` extra allows."""

STARLETTE_FLOOR: Final = "1.7.0"
"""The oldest Starlette the `fastapi` and `starlette` extras allow."""

_VERSION: Final = re.compile(r"v?(\d+(?:\.\d+)*)(.*)")
"""A version: its release numbers, then any suffix such as `rc1`."""

_PRERELEASE: Final = re.compile(r"a|b|c|rc|dev|pre")
"""A suffix marking a pre-release or a development build."""


def _rank(version: str) -> tuple[tuple[int, ...], bool]:
    """Return how `version` sorts against another version.

    Trailing zeros are dropped, so `1.7` and `1.7.0` rank the same. A
    pre-release or a development build ranks below its release. A local
    part, like `+local`, is ignored. Raises `ValueError` when `version`
    starts with no number.
    """
    parsed = _VERSION.fullmatch(version.split("+", 1)[0].lower())
    if parsed is None:
        msg = f"Version {version!r} has no release number"
        raise ValueError(msg)
    release = tuple(int(part) for part in parsed.group(1).split("."))
    while release and release[-1] == 0:
        release = release[:-1]
    suffix = parsed.group(2)
    final = "post" in suffix or _PRERELEASE.search(suffix) is None
    return release, final


def _version_of(framework: ModuleType) -> str | None:
    """Return the module's `__version__`, else its installed version.

    Returns `None` when neither exists or the version has no number.
    """
    version = getattr(framework, "__version__", None)
    if not isinstance(version, str):
        try:
            version = installed_version(framework.__name__)
        except PackageNotFoundError:
            return None
    try:
        _rank(version)
    except ValueError:
        return None
    return version


def require(framework: ModuleType, floor: str, *, extra: str) -> None:
    """Raise `DependencyVersionError` when `framework` is older than `floor`.

    The version is the module's `__version__`, or the installed version of
    the distribution of the same name when the module has none. Versions
    compare as a `>=` pin does: number by number, `1.7` equal to `1.7.0`,
    and a pre-release of the floor refused. A version that cannot be read
    is refused too.
    """
    version = _version_of(framework)
    if version is not None and _rank(version) >= _rank(floor):
        return
    raise DependencyVersionError(
        module=framework.__name__,
        version=version,
        floor=floor,
        extra=extra,
    )
