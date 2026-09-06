"""Filesystem config backend."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self, cast

from typing_extensions import Doc

from grelmicro._config import env_segment
from grelmicro._json import json_dumps_str, json_loads
from grelmicro.errors import SettingsValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from os import PathLike
    from types import TracebackType

    from grelmicro._json import JSONEncodable


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
      `GREL_LOCK_CART_LEASE_DURATION=30`. A name is normalised the way it
      is on its way into a prefix, so `cart.v2` reads as `CART_V2`.

      A nested mapping is written both ways, because the document does
      not say which it is: as JSON under its own name, for a field that
      takes a mapping such as `include: {"/products/*": 60}` or
      `headers: {authorization: ...}`, and walked as a level as well.
      Whichever name reaches a field is the one that fills it, and the
      other matches nothing. A list is written as JSON.
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
    """Walk a mapping, writing every reading of it a field could want.

    A nested mapping is two things at once and the document does not say
    which: `lock: {cart: {lease_duration: 30}}` is a level of nesting,
    while `include: {"/products/*": 60}` is one field's value. Deciding
    by the shape of the keys guesses wrong both ways. `cart.v2` is a
    valid instance name and not a variable segment, and an OTel header
    name looks exactly like one.

    So both readings are written. The mapping is written as JSON under
    its own name, and walked as a level as well. The two never take the
    same name, because walking always adds a segment. Whichever one names
    a field fills it, and the other matches nothing and is ignored, which
    is what an unmatched key already gets. A walked reading of a value
    mapping is therefore expected, not a mistake: `include` names the
    field, and `include_products` under it names nothing.

    Two sibling keys can still normalise to one segment, `a-b` and `a_b`
    both to `A_B`, and the last read wins. Two instances named that way
    already share one address at construction, so the collision is the
    one [the configuration contract](../architecture/config.md)
    describes rather than a new one.
    """
    for key, value in data.items():
        segment = _segment(key)
        if segment is None:
            # No variable name can be built from it, so the level below
            # is unreachable. The mapping above still wrote itself as
            # JSON, which is the reading that names a field here.
            continue
        name = f"{prefix}_{segment}" if prefix else segment
        if isinstance(value, dict):
            encoded = _encoded(value)
            if encoded is not None:
                result[name] = encoded
            _flatten_into(result, value, prefix=name)
        else:
            result[name] = _stringify(value)


def _segment(key: object) -> str | None:
    """Return the variable-name segment this key writes to.

    The same normalisation an instance name goes through on its way into
    a prefix, so a `Lock("cart.v2")` reading `GREL_LOCK_CART_V2_*` is
    filled by a document that writes `cart.v2` as it was named. `None`
    for a key no segment can be built from, such as a path pattern.
    """
    try:
        return env_segment(str(key))
    except SettingsValidationError:
        return None


def _encoded(value: dict[Any, Any]) -> str | None:
    """Return the mapping as JSON, or `None` when it does not encode.

    A document may hold a value JSON has no form for, a date above all.
    That reading is simply not offered, and walking the mapping as a
    level still is.
    """
    try:
        return json_dumps_str(cast("JSONEncodable", value))
    except TypeError:
        return None


def _stringify(value: object) -> str:
    """Stringify a value the way the field reading it parses.

    A scalar is written as it reads, with bool lowercased. A sequence or
    a mapping is written as JSON, which is what pydantic-settings parses
    a complex field from. `str()` would render a list as `['/a']`, whose
    quotes are not JSON, so the field it fills would refuse it.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json_dumps_str(cast("JSONEncodable", value))
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
