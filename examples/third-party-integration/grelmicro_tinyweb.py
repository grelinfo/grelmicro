"""The grelmicro integration for tinyweb.

`micro.install(app)` finds it through the `grelmicro.integrations` entry
point that this package's `pyproject.toml` declares, keyed by `tinyweb`, the
top-level module of `TinyWeb`. One `_declare` function tells grelmicro what
each route requires, both to gate it and to list it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from grelmicro import Grelmicro, GrelmicroMiddleware
from grelmicro.http import RouteDeclaration

if TYPE_CHECKING:
    from tinyweb import Route, TinyWeb

    from grelmicro.http import Gate


def install(app: TinyWeb, micro: Grelmicro, *, ambient: bool = True) -> None:
    """Open `micro` inside the app's lifespan, and bind it to each request."""
    previous = app.lifespan

    @asynccontextmanager
    async def lifespan(app: TinyWeb) -> AsyncGenerator[None]:
        async with previous(app), micro:
            yield

    app.lifespan = lifespan
    if ambient and not is_bound(app):
        app.middleware.insert(0, (GrelmicroMiddleware, {"micro": micro}))


def is_bound(app: TinyWeb) -> bool:
    """Return whether `install` bound grelmicro to each request of `app`."""
    return any(cls is GrelmicroMiddleware for cls, _ in app.middleware)


def install_middleware(app: TinyWeb, components: list[Any]) -> None:
    """Add each component's middleware inside the binding, the first outermost."""
    app.middleware.extend(
        component.asgi_middleware() for component in components
    )


def route_declarations(app: TinyWeb) -> list[RouteDeclaration]:
    """List what each route requires."""
    return [_declare(route) for route in app.routes]


def install_route_gate(app: TinyWeb, gate: Gate) -> None:
    """Dispatch each route to the gate built from its declaration."""
    for route in app.routes:
        route.app = gate(route.app, _declare(route))


def _declare(route: Route) -> RouteDeclaration:
    return RouteDeclaration(
        route.path,
        methods=route.methods,
        anonymous=route.anonymous,
        scopes=route.scopes,
        cache=route.cache,
        own_checks=route.own_checks,
    )
