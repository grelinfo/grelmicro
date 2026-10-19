"""Litestar's plugin registry as grelmicro joins it once the app is built.

Litestar hands each route it registers to every `ReceiveRoutePlugin` in
`app.plugins.receive_route`, a tuple it fills once from
`Litestar(plugins=[...])`. `micro.install(app)` runs after that, so
`watch_routes` adds its plugin to the tuple. Every private part of
Litestar grelmicro relies on is read here alone. `require(app)` fails
install when a Litestar release moved one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from litestar.plugins import ReceiveRoutePlugin

if TYPE_CHECKING:
    from litestar import Litestar

__all__ = ["require", "watch_routes"]

_MISSING: Final = object()
"""Marks a part the registry does not carry."""


def require(app: Litestar) -> None:
    """Fail when Litestar no longer keeps its route plugins where grelmicro joins them.

    Raises:
        RuntimeError: Naming each missing part.
    """
    plugins = getattr(app.plugins, "receive_route", _MISSING)
    missing = [
        name
        for absent, name in (
            (plugins is _MISSING, "PluginRegistry.receive_route"),
            (
                plugins is not _MISSING and not isinstance(plugins, tuple),
                "PluginRegistry.receive_route as a tuple",
            ),
            (
                isinstance(plugins, tuple)
                and not all(
                    isinstance(plugin, ReceiveRoutePlugin) for plugin in plugins
                ),
                "PluginRegistry.receive_route holding ReceiveRoutePlugin alone",
            ),
        )
        if absent
    ]
    if missing:
        msg = (
            f"Litestar's plugin registry has no {', '.join(missing)}, which "
            "micro.install(app) joins to describe the routes registered "
            "after it. Install a Litestar release grelmicro supports."
        )
        raise RuntimeError(msg)


def watch_routes(app: Litestar, plugin: ReceiveRoutePlugin) -> None:
    """Hand `plugin` each route registered on the app from now on.

    It runs after Litestar's own, so it sees a schema Litestar is ready
    to build anew.

    Raises:
        RuntimeError: When Litestar moved its route plugins.
    """
    require(app)
    app.plugins.receive_route = (*app.plugins.receive_route, plugin)
