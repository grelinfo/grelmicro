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
from typing import TYPE_CHECKING, Any, Protocol, cast

from grelmicro.errors import (
    AdapterNotRegisteredError,
    ProviderNotRegisteredError,
)

if TYPE_CHECKING:
    from grelmicro.providers._base import Provider

PROVIDER_GROUP = "grelmicro.providers"

INTEGRATION_GROUP = "grelmicro.integrations"
"""Maps a web framework's top-level module name to its integration module.

`micro.install(app)` walks the app class's MRO and looks up each class's root
module here, so a `FastAPI` subclass defined in the user's own package still
resolves. Only the matching integration is imported, which keeps `install`
from loading every framework grelmicro knows about.

An integration module exposes `install(app, micro, *, ambient)` and
`is_bound(app)`.
"""


def adapter_group(kind: str) -> str:
    """Return the entry-point group name for a component kind."""
    return f"grelmicro.{kind}.adapters"


class Integration(Protocol):
    """What an integration module exposes to `Grelmicro`.

    A third-party package ships one of these and registers it under
    `INTEGRATION_GROUP`, keyed by the framework's top-level module name.

    Both signatures are frozen. grelmicro never adds an argument to
    `install`, so an integration written today keeps working. A new
    capability arrives as a new optional module attribute that grelmicro
    feature-detects with `getattr`.

    `install_error_responses(app, errors)` is the first of those, where
    `errors` is the registered `ErrorResponses` component. An integration
    that defines it answers every error in the format that component
    carries, and one that does not is skipped, which is what a framework
    serving no HTTP wants.

    `install_middleware(app, components)` is the second, where `components`
    are the registered components carrying `asgi_middleware()`. An
    integration that defines it adds each middleware the way its framework
    takes one, and one that does not is skipped the same way.
    """

    def install(
        self,
        app: Any,  # noqa: ANN401
        micro: Any,  # noqa: ANN401
        *,
        ambient: bool = True,
    ) -> None:
        """Wire the framework lifecycle and the per-handler binding."""
        ...

    def is_bound(self, app: Any) -> bool:  # noqa: ANN401
        """Return whether `install` added the binding middleware to `app`."""
        ...


def load_integration(app: object) -> Integration | None:
    """Return the integration module for `app`, or `None` when none matches.

    Walks the app class's MRO so a subclass resolves through the framework it
    inherits from. The first root module registered under
    `INTEGRATION_GROUP` wins, and only that module is imported.
    """
    eps = {ep.name: ep for ep in entry_points(group=INTEGRATION_GROUP)}
    for klass in type(app).__mro__:
        ep = eps.get(klass.__module__.partition(".")[0])
        if ep is not None:
            return cast("Integration", ep.load())
    return None


def integration_names() -> list[str]:
    """Return the framework names an integration is registered for."""
    return sorted(ep.name for ep in entry_points(group=INTEGRATION_GROUP))


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
