"""Lazy discovery of Providers and Adapters via entry-point groups.

Third-party packages register Providers and Adapters under grelmicro's
entry-point groups so they resolve by short name without grelmicro depending
on the vendor. First-party Providers and Adapters travel the same path: there
is no special case.

- `grelmicro.providers` maps a vendor short name to a `Provider` class.
- `grelmicro.{kind}.adapters` maps a short name to an Adapter class for one
  component kind (`coordination`, `coordination.election`, `cache`,
  `ratelimiter`, `circuitbreaker`).

Listing entry points does not import anything. The target module loads only
when `load_provider` or `load_adapter` resolves a name, via `ep.load()`.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from grelmicro.errors import (
    AdapterNotRegisteredError,
    ProviderNotRegisteredError,
)

if TYPE_CHECKING:
    from grelmicro.providers._base import Provider

PROVIDER_GROUP = "grelmicro.providers"


def adapter_group(kind: str) -> str:
    """Return the entry-point group name for a component kind."""
    return f"grelmicro.{kind}.adapters"


def load_provider(short_name: str) -> type[Provider]:
    """Load the `Provider` class registered under `short_name`.

    Raises:
        ProviderNotRegisteredError: No provider matches the short name.
    """
    eps = entry_points(group=PROVIDER_GROUP)
    for ep in eps:
        if ep.name == short_name:
            return ep.load()
    raise ProviderNotRegisteredError(short_name, sorted(ep.name for ep in eps))


def load_adapter(kind: str, short_name: str) -> type:
    """Load the Adapter class registered under `short_name` for `kind`.

    Raises:
        AdapterNotRegisteredError: No adapter matches the short name.
    """
    eps = entry_points(group=adapter_group(kind))
    for ep in eps:
        if ep.name == short_name:
            return ep.load()
    raise AdapterNotRegisteredError(
        kind, short_name, sorted(ep.name for ep in eps)
    )
