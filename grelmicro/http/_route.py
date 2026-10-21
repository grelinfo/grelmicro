"""The route a request took."""

from collections.abc import MutableMapping
from typing import Annotated, Any

from typing_extensions import Doc

from grelmicro import _paths
from grelmicro._paths import ROUTE_KEY, ROUTE_READER_KEY
from grelmicro.errors import RouteReaderError


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
    FastAPI, a route inside an app mounted with `app.mount` reads as the
    mount's own template.

    A URL no route matches returns `None`. Under a mount, it returns the
    template of the mount that took it, such as `/admin/{path}`. A request
    answered before routing, such as a refused token or a cached response,
    returns the route its path reaches.

    Before routing it matches the path again, so it costs a route lookup.

    Raises:
        RouteReaderError: If the scope carries no route reader, because
            `micro.install(app)` did not wire the app, or because a
            wrapper reads it before the installed app runs.
    """
    if ROUTE_KEY not in scope and ROUTE_READER_KEY not in scope:
        raise RouteReaderError
    return _paths.route_template(scope)
