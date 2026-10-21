"""The route a request took."""

from collections.abc import MutableMapping
from typing import Annotated, Any

from typing_extensions import Doc

from grelmicro import _paths
from grelmicro.errors import AppNotInstalledError


def route_template(
    scope: Annotated[
        MutableMapping[str, Any], Doc("The ASGI scope of the request.")
    ],
) -> str | None:
    """Return the template of the route the request reached.

    It is the route the access log writes as `http.route`, such as
    `/api/meals/v1/recipes/{recipe_id}` for `/api/meals/v1/recipes/7`. It
    carries every prefix the route was included under, and it starts with
    the request's `root_path`. On Starlette it joins nested mounts. On
    FastAPI, a route of a FastAPI app mounted with `app.mount` reads as
    its full template, such as `/mm/r/{x}`. A Starlette app or `Mount`
    mounted in FastAPI reads as the mount's own template, such as
    `/mm/{path}`.

    A URL no route matches returns `None`. Under a mount, it returns the
    template of the mount that took it, such as `/admin/{path}`. A request
    answered before routing, such as a refused token or a cached response,
    returns the route its path reaches. On Starlette, a slash redirect
    returns `None`, where the access log writes the route it redirects to.

    Before routing it matches the path again, so it costs a route lookup.

    Raises:
        AppNotInstalledError: If `micro.install(app)` did not wire the app,
            or if a wrapper calls it before the installed app runs.
    """
    if _paths.ROUTE_READER_KEY not in scope:
        raise AppNotInstalledError
    return _paths.route_template(scope)
