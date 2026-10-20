"""How a Litestar router reads the path of a request, and which handler it matched.

Importing this module has `route_path` read the path of a request a
Litestar app serves as Litestar's router reads it. It does not import
Litestar, so an integration of another framework imports it to read a
Litestar app mounted under its own.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Annotated, Any, Final

from typing_extensions import Doc

from grelmicro._paths import read_paths_with

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping

__all__ = [
    "litestar_mount",
    "litestar_owned_handler",
    "litestar_route_handler",
    "litestar_route_path",
]

_ROUTE_HANDLER: Final = "route_handler"
"""The key Litestar's router writes the handler it matched under."""


def litestar_route_path(
    app: Annotated[Any, Doc("The Litestar app in `scope['app']`.")],  # noqa: ANN401
    scope: Annotated[
        MutableMapping[str, Any], Doc("The ASGI scope of the request.")
    ],
    path: Annotated[str, Doc("The path the scope holds.")],
    root_path: Annotated[str, Doc("The root path the scope holds.")],
) -> str:
    """Return the path the router of `app` matches the request with.

    Before routing, Litestar takes the root path off where it first
    appears, and normalizes the path whether it had one or not. Behind its
    router, the path is the one the router wrote back, with the root path
    off already. A mount's is only what is left inside the mount, so the
    mount's own path is joined back to it. A handler the app does not own,
    such as the mount of an outer Litestar app, is read as no handler, and
    the path as before routing.

    Raises:
        RuntimeError: If Litestar routed the request to a mount its router
            does not list, so the path the mount serves is unknown.
    """
    handler = litestar_owned_handler(app, scope.get(_ROUTE_HANDLER))
    if handler is None:
        routed = path.split(root_path, maxsplit=1)[-1] if root_path else path
        return _normalize()(routed)
    if getattr(handler, "is_mount", False):
        return _normalize()(f"{litestar_mount(app, handler)}{path}")
    return path


def litestar_route_handler(
    scope: Annotated[
        MutableMapping[str, Any], Doc("The ASGI scope of the request.")
    ],
) -> Any | None:  # noqa: ANN401
    """Return the handler the router of the Litestar app serving the request matched.

    `None` when the app in `scope["app"]` is not a Litestar app, or when the
    handler in the scope is not one that app owns, such as the mount of an
    outer Litestar app before the app's own router matched.
    """
    litestar = _serving_litestar(scope)
    if litestar is None:
        return None
    return litestar_owned_handler(litestar, scope.get(_ROUTE_HANDLER))


def _serving_litestar(scope: MutableMapping[str, Any]) -> Any | None:  # noqa: ANN401
    """Return the Litestar app in `scope["app"]`, or `None` for any other app.

    Litestar's own key stays in the scope of an app mounted under it, so it
    names the app serving the request only when it is that same app.
    """
    litestar = scope.get("litestar_app")
    if litestar is None or litestar is not scope.get("app"):
        return None
    return litestar


def litestar_owned_handler(app: Any, handler: Any) -> Any | None:  # noqa: ANN401
    """Return `handler` when the Litestar app `app` owns it, or `None`.

    Litestar gives each handler it registers the router it was registered
    on as its owner, and each router the one above it, up to the app.
    """
    if handler is None:
        return None
    owner = handler.owner
    while owner is not None:
        if owner is app:
            return handler
        owner = owner.owner
    return None


def litestar_mount(app: Any, handler: Any) -> str:  # noqa: ANN401
    """Return the path a Litestar app mounts `handler` at, router prefixes included.

    Raises:
        RuntimeError: If the router lists no mount for the handler.
    """
    mounts = app.asgi_router._mount_routes  # noqa: SLF001
    for mount, node in mounts.items():
        if any(entry[1] is handler for entry in node.asgi_handlers.values()):
            return mount
    msg = (
        "Litestar routed the request to a mount its router does not list, "
        "so the path it serves is unknown."
    )
    raise RuntimeError(msg)


@functools.cache
def _normalize() -> Callable[[str], str]:
    """Return how Litestar normalizes a path, looked up once."""
    from litestar.utils import normalize_path  # noqa: PLC0415

    return normalize_path


read_paths_with(
    "litestar.app.Litestar", litestar_route_path, writes=(_ROUTE_HANDLER,)
)
