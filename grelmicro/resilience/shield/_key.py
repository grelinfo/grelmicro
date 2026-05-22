"""Cache key helpers."""

from __future__ import annotations

import hashlib
from typing import Any

__all__ = ["default_cache_key", "stable_hash"]


def stable_hash(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Return a 16-character stable hex digest for `(args, kwargs)`.

    Hashes `repr(args) + repr(sorted(kwargs.items()))` with SHA-256
    and returns the first 16 hex characters. Non-hashable arguments
    are accepted because the digest is computed over their `repr`.
    """
    payload = repr(args) + repr(sorted(kwargs.items()))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:16]


def default_cache_key(
    name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    """Return the default cache key for a `Shield` call.

    Format: `f"{name}:{stable_hash(args, kwargs)}"`.
    """
    return f"{name}:{stable_hash(args, kwargs)}"
