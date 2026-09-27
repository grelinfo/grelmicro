"""Decide whether a release publishes grelmicro-core, and refuse a wrong one.

`grelmicro[jwt]` requires the crate version in `rust/grelmicro-core`, and the
Release workflow publishes that version first when PyPI does not have it.
`plan` makes the decision and refuses three states that would ship a
`grelmicro` whose `jwt` extra cannot resolve or resolves to another crate:

- The `jwt` extra requires a version other than the crate's.
- The crate version is on PyPI, but the crate changed since it was published.
- The crate version is new, but not above the latest one on PyPI.

A published version is compared with its source distribution, which holds the
crate files byte for byte, so the check needs no tag and no Rust toolchain.

`released` answers whether PyPI already holds exactly this crate, which is
how CI installs the published wheel rather than compiling an untouched crate.

Standard library only, so it runs before any dependency is installed.

Run via `just release-check`, CI and the Release workflow.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

ROOT = Path(__file__).resolve().parent.parent
CRATE = ROOT / "rust" / "grelmicro-core"
PYPROJECT = ROOT / "pyproject.toml"
PYPI_JSON = "https://pypi.org/pypi/grelmicro-core/json"

REQUIREMENT = re.compile(r"^grelmicro-core>=(?P<version>\S+)$")
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
BUILD_OUTPUT = frozenset({"target", "__pycache__"})


class ReleaseError(Exception):
    """The crate, the pin and PyPI disagree in a way a release would ship."""


def crate_version(cargo_toml: Path) -> str:
    """Return the `[package]` version of the crate."""
    with cargo_toml.open("rb") as file:
        return tomllib.load(file)["package"]["version"]


def required_core(pyproject: Path) -> str:
    """Return the minimum grelmicro-core version the `jwt` extra requires."""
    with pyproject.open("rb") as file:
        extra = tomllib.load(file)["project"]["optional-dependencies"]["jwt"]
    for requirement in extra:
        match = REQUIREMENT.match(requirement.replace(" ", ""))
        if match:
            return match.group("version")
    msg = f"the jwt extra in {pyproject} names no grelmicro-core>=X.Y.Z"
    raise ReleaseError(msg)


def source_files(crate: Path) -> dict[str, bytes]:
    """Return the crate files an sdist ships, keyed by relative path."""
    files: dict[str, bytes] = {}
    for path in sorted(crate.rglob("*")):
        relative = path.relative_to(crate)
        if any(
            part in BUILD_OUTPUT or part.startswith(".")
            for part in relative.parts
        ):
            continue
        if path.is_file():
            files[relative.as_posix()] = path.read_bytes()
    return files


def sdist_files(archive: bytes) -> dict[str, bytes]:
    """Return the crate files in an sdist, keyed like `source_files`."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            _, _, name = member.name.partition("/")
            extracted = tar.extractfile(member)
            if extracted is None or name == "PKG-INFO":
                continue
            files[name] = extracted.read()
    return files


def _semver(version: str) -> tuple[int, int, int]:
    match = SEMVER.match(version)
    if match is None:
        msg = f"grelmicro-core {version} is not a plain X.Y.Z version"
        raise ReleaseError(msg)
    major, minor, patch = (int(group) for group in match.groups())
    return major, minor, patch


def plan(
    *,
    version: str,
    required: str,
    released: Collection[str],
    released_source: Mapping[str, bytes] | None,
    source: Mapping[str, bytes],
) -> bool:
    """Return whether the release publishes grelmicro-core `version`.

    `released` holds every version on PyPI, and `released_source` the sdist
    files of `version` when PyPI has them. Raises `ReleaseError` when the
    release would ship a pin that cannot resolve to the tested crate.
    """
    if required != version:
        msg = (
            f"the jwt extra requires grelmicro-core>={required}, but the "
            f"crate is {version}. Set it to grelmicro-core>={version}."
        )
        raise ReleaseError(msg)
    if version in released and released_source is not None:
        changed = sorted(
            name
            for name in source.keys() | released_source.keys()
            if source.get(name) != released_source.get(name)
        )
        if changed:
            msg = (
                f"rust/grelmicro-core changed since grelmicro-core {version} "
                f"was published ({', '.join(changed)}). Bump the version in "
                "Cargo.toml and the jwt extra to the next one."
            )
            raise ReleaseError(msg)
        return False
    if version in released:
        return True
    current = _semver(version)
    for other in released:
        if SEMVER.match(other) and _semver(other) >= current:
            msg = (
                f"grelmicro-core {version} is not above {other}, which is "
                "already on PyPI. Bump the version in Cargo.toml and the "
                "jwt extra above it."
            )
            raise ReleaseError(msg)
    return True


def fetch(version: str) -> tuple[set[str], bytes | None]:
    """Return every version on PyPI, and the sdist of `version` if any."""
    try:
        with urllib.request.urlopen(PYPI_JSON, timeout=30) as response:
            releases = json.load(response)["releases"]
    except urllib.error.HTTPError as error:
        if error.code == 404:  # noqa: PLR2004
            return set(), None
        raise
    released = {name for name, files in releases.items() if files}
    for file in releases.get(version, []):
        if file["packagetype"] == "sdist":
            with urllib.request.urlopen(file["url"], timeout=60) as response:  # noqa: S310
                return released, response.read()
    return released, None


def _decide() -> tuple[str, bool]:
    version = crate_version(CRATE / "Cargo.toml")
    released, archive = fetch(version)
    publish = plan(
        version=version,
        required=required_core(PYPROJECT),
        released=released,
        released_source=None if archive is None else sdist_files(archive),
        source=source_files(CRATE),
    )
    return version, publish


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print version= and publish= step outputs")
    sub.add_parser("released", help="exit 0 if PyPI holds exactly this crate")
    args = parser.parse_args(argv)
    if args.command == "released":
        try:
            _, needs_publish = _decide()
        except ReleaseError:
            return 1
        return 1 if needs_publish else 0
    try:
        version, publish = _decide()
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"version={version}")
    print(f"publish={'true' if publish else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
