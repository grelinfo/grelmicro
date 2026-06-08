"""Filesystem config backend."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self

from typing_extensions import Doc

from grelmicro._json import json_loads

if TYPE_CHECKING:
    from collections.abc import Mapping
    from os import PathLike
    from types import TracebackType


class FileConfigAdapter:
    """Read configuration from the filesystem.

    Built for mounted configuration: a Kubernetes ConfigMap or Secret, a
    Docker config or secret, or any directory a sidecar writes to. Three
    shapes are accepted, picked from what is on disk:

    - A directory: every file is one key, the filename is the key and the
      file content is the value. This is how Kubernetes mounts a ConfigMap
      or Secret as a volume. Entries whose name starts with `..` are
      skipped, so the `..data` symlink Kubernetes maintains is ignored.
    - A `.json` file: a flat JSON object of keys to values.
    - Any other file: `KEY=VALUE` lines, blank lines and `#` comments
      ignored, matching a `.env` file.

    Keys are the same `GREL_...` names components resolve from the
    environment. The adapter remembers what it last read and returns
    `None` from `load` when nothing changed, so an unchanged mount costs
    one read and no reconfiguration.
    """

    def __init__(
        self,
        path: Annotated[
            str | PathLike[str],
            Doc(
                """
                The directory or file to read. A mounted ConfigMap or Secret
                is a directory. A single `.env` or `.json` file works too.
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
        """Read the current mapping, or `None` when unchanged or absent."""
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
        if path.suffix == ".json":
            data = json_loads(path.read_text())
            if not isinstance(data, dict):
                msg = f"{path} must contain a JSON object of keys to values"
                raise ValueError(msg)
            return {str(k): str(v) for k, v in data.items()}
        return _parse_env(path.read_text())


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
