"""Hand a job body an object the test keeps, through a JSON argument."""

from typing import Any
from uuid import uuid4

_SHARED: dict[str, Any] = {}


def share(value: object) -> str:
    """Keep `value`, and return the key a job body reads it back with."""
    key = uuid4().hex
    _SHARED[key] = value
    return key


def shared(key: str) -> Any:  # noqa: ANN401
    """Return the object kept under `key`."""
    return _SHARED[key]
