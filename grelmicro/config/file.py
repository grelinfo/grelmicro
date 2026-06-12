"""Filesystem config backend."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self

from typing_extensions import Doc

from grelmicro._json import json_loads

if TYPE_CHECKING:
    from collections.abc import Mapping
    from os import PathLike
    from types import TracebackType


class FileConfigAdapter:
    """Read configuration from the filesystem.

    Built for mounted configuration: a Kubernetes ConfigMap or Secret, a
    Docker config or secret, or any directory a sidecar writes to. The
    shape is picked from what is on disk:

    - A directory: every file is one key, the filename is the key and the
      file content is the value. This is how Kubernetes mounts a ConfigMap
      or Secret as a volume. Entries whose name starts with `..` are
      skipped, so the `..data` symlink Kubernetes maintains is ignored.
    - A `.json`, `.yaml`, `.yml`, or `.toml` file: a mapping document.
      Either a flat mapping of `GREL_...` keys to scalar values, or a
      nested mapping whose segments join with `_` and uppercase, so
      `grel: {lock: {cart: {lease_duration: 30}}}` reads as
      `GREL_LOCK_CART_LEASE_DURATION=30`.
    - Any other file: `KEY=VALUE` lines, blank lines and `#` comments
      ignored, matching a `.env` file.

    Keys are the same `GREL_...` names components resolve from the
    environment. The adapter remembers what it last read and returns
    `None` from `load` when nothing changed, so an unchanged mount costs
    one read and no reconfiguration.

    An absent path reads as an empty mapping rather than an error, so a
    mount that is not present yet is not a failure. A path that exists but
    cannot be read raises `OSError`, and a mapping document whose content
    is not a mapping raises `ValueError`. `ExternalConfig` catches both
    and keeps the last good config.

    Reading `.yaml` or `.yml` needs PyYAML, installed with the `yaml`
    extra. The import is lazy, so a `DependencyNotFoundError` is raised
    only when a YAML file is actually read.
    """

    def __init__(
        self,
        path: Annotated[
            str | PathLike[str],
            Doc(
                """
                The directory or file to read. A mounted ConfigMap or Secret
                is a directory. A single `.env`, `.json`, `.yaml`, `.yml`, or
                `.toml` file works too.
                """,
            ),
        ],
    ) -> None:
        """Initialize the filesystem config backend."""
        self._path = Path(path)
        self._last: Mapping[str, str] | None = None
        self._loaded = False

    async def __aenter__(self) -> Self:
        """Open the backend (no resources to acquire)."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend (nothing to release)."""

    async def load(self) -> Mapping[str, str] | None:
        """Read the current mapping, or `None` when unchanged.

        Raises:
            OSError: The path exists but cannot be read.
            ValueError: A mapping document does not hold a mapping.
            DependencyNotFoundError: A `.yaml` or `.yml` file is read
                without PyYAML installed.
        """
        current = self._read()
        if self._loaded and current == self._last:
            return None
        self._loaded = True
        self._last = current
        return current

    def _read(self) -> Mapping[str, str]:
        """Read the raw mapping from disk, empty when the path is absent."""
        path = self._path
        if path.is_dir():
            return {
                entry.name: entry.read_text().rstrip("\n")
                for entry in path.iterdir()
                if not entry.name.startswith("..") and entry.is_file()
            }
        if not path.is_file():
            return {}
        suffix = path.suffix
        if suffix == ".json":
            return _flatten_document(json_loads(path.read_text()), path)
        if suffix in {".yaml", ".yml"}:
            return _flatten_document(_yaml_load(path.read_text()), path)
        if suffix == ".toml":
            return _flatten_document(tomllib.loads(path.read_text()), path)
        return _parse_env(path.read_text())


def _yaml_load(text: str) -> object:
    """Parse YAML with PyYAML, raising `DependencyNotFoundError` when absent."""
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        from grelmicro.errors import DependencyNotFoundError  # noqa: PLC0415

        raise DependencyNotFoundError(module="pyyaml") from None
    return yaml.safe_load(text)


def _flatten_document(data: object, path: Path) -> dict[str, str]:
    """Flatten a mapping document into `GREL_...` keys to string values.

    A flat mapping stringifies its values. A nested mapping joins its
    segments with `_`, uppercases, and stringifies the leaf scalars.
    """
    if not isinstance(data, dict):
        msg = f"{path} must contain a mapping of keys to values"
        raise ValueError(msg)  # noqa: TRY004
    result: dict[str, str] = {}
    _flatten_into(result, data, prefix="")
    return result


def _flatten_into(
    result: dict[str, str], data: dict[Any, Any], *, prefix: str
) -> None:
    """Walk a mapping, recursing into nested mappings, writing leaf scalars."""
    for key, value in data.items():
        name = f"{prefix}_{key}" if prefix else str(key)
        if isinstance(value, dict):
            _flatten_into(result, value, prefix=name)
        else:
            result[name.upper()] = _stringify(value)


def _stringify(value: object) -> str:
    """Stringify a scalar, rendering bool as lowercase `true`/`false`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_env(text: str) -> dict[str, str]:
    """Parse `KEY=VALUE` lines, ignoring blanks and `#` comments."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("\"'")
    return result
