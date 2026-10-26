"""The authentication declarations of the Litestar integration.

`grelmicro.integrations.litestar` loads this module when `Anonymous`,
`Authenticated` or `current_token` is first read, and when it wires
`AuthenticatedRequests`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

from litestar.exceptions import HTTPException
from typing_extensions import Doc

from grelmicro.http._authentication import (
    ANONYMOUS_OPT,
    refuse_routes_at_metadata,
    resource_metadata_of,
)
from grelmicro.http._requirement import AUTHENTICATED, Requirement
from grelmicro.integrations.litestar import _routed_middleware

if TYPE_CHECKING:
    from collections.abc import (
        Awaitable,
        Callable,
        MutableMapping,
        Sequence,
    )

    from litestar import Litestar
    from litestar.connection import ASGIConnection
    from litestar.handlers.base import BaseRouteHandler

    from grelmicro.security.principal import VerifiedToken

    Scope = MutableMapping[str, Any]

__all__ = [
    "Anonymous",
    "Authenticated",
    "current_token",
    "route_resource_metadata",
]


def Anonymous() -> dict[str, Any]:  # noqa: N802
    """Serve this route without a credential.

    Every route is authenticated once `AuthenticatedRequests` is registered.
    Pass this as the handler's `opt` on the one that is public:

    ```python
    from litestar import get

    from grelmicro.integrations.litestar import Anonymous


    @get("/catalog", opt=Anonymous())
    async def catalog() -> list[Product]: ...
    ```

    It is a mapping, so it merges with options of your own:
    `opt={**Anonymous(), "tag": "public"}`. `micro.install(app)` reads it off
    every handler, per method, so a public read keeps its writes
    authenticated, and the app is read again when it starts.

    A token sent to it is verified: a valid one is the caller in
    `request.user`, and one that does not verify is answered `401`.

    It is `{"exclude_from_auth": True}`, the key Litestar's own
    authentication middleware reads, so a handler written for that one is
    public here too.

    Read more in the [Authentication](../http/authentication.md) docs.
    """
    return {ANONYMOUS_OPT: True}


def Authenticated(  # noqa: N802
    *,
    scopes: Annotated[
        Sequence[str],
        Doc("Scopes the caller must hold, every one of them."),
    ] = (),
) -> Callable[[ASGIConnection, BaseRouteHandler], Awaitable[None]]:
    """Require an authenticated caller holding every scope named.

    A Litestar guard:

    ```python
    from litestar import delete

    from grelmicro.integrations.litestar import Authenticated


    @delete(
        "/orders/{order_id:int}",
        guards=[Authenticated(scopes=["orders:write"])],
    )
    async def cancel(order_id: int) -> None: ...
    ```

    A caller with no credential is answered `401`, and one lacking a scope
    `403`, whose `WWW-Authenticate` challenge names the scopes. It
    needs a registered `AuthenticatedRequests`, which verifies the token
    before the handler runs.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        TypeError: If `scopes` is a single string.
        ValueError: If a scope is not an OAuth scope token.
    """
    requirement = Requirement(scopes)

    async def authenticated(
        connection: ASGIConnection,
        handler: BaseRouteHandler,  # noqa: ARG001
    ) -> None:
        """Refuse a caller that is not authenticated or lacks a scope."""
        requirement.caller(cast("Scope", connection.scope))

    return requirement.declare(authenticated)


def current_token(
    connection: Annotated[
        ASGIConnection,
        Doc("The request or websocket the handler was handed."),
    ],
) -> VerifiedToken:
    """Return the bearer token the request presented, once it verified.

    ```python
    from litestar import Request, post

    from grelmicro.integrations.litestar import current_token


    @post("/orders")
    async def create_order(request: Request) -> dict[str, str]:
        token = current_token(request)
        response = await payments.post(
            "/charges", auth=payments_for_user.auth(token)
        )
        return response.json()
    ```

    `AuthenticatedRequests` leaves it on every request it authenticates.
    Read it to act for the caller, such as exchanging it with a
    `TokenExchange`. Its `repr` never shows the token.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        AuthenticationRequiredError: If the request carried no token that
            `AuthenticatedRequests` verified, such as one on a handler
            declaring `Anonymous()`.
    """
    return AUTHENTICATED.token(cast("Scope", connection.scope))


def route_resource_metadata(app: Litestar, component: Any) -> None:  # noqa: ANN401
    """Add a route at the path the protected resource metadata is served at.

    A middleware passed to `Litestar(middleware=[...])` runs behind the
    router, which answers `404` to a path no route matches before that
    middleware sees it. The route serves the document itself, so it is
    found however the middleware was placed. A route the app declares at
    that path is refused, since it would never run. One reaching the path
    through a parameter routes the request on to the middleware already,
    so none is added then.

    Raises:
        TypeError: If the app declares a route at the metadata path.
    """
    from litestar import asgi  # noqa: PLC0415

    middleware, options = component.asgi_middleware()
    declared = [
        arguments
        for declared_class, arguments in _routed_middleware(app)
        if declared_class is middleware
    ]
    for described in declared or [options]:
        metadata = resource_metadata_of(described)
        if metadata is None:
            continue
        refuse_routes_at_metadata(app, metadata)
        if _routes(app, metadata.route):
            continue
        app.register(
            asgi(metadata.route, opt=Anonymous(), copy_scope=False)(
                metadata.document()
            )
        )


def _routes(app: Litestar, path: str) -> bool:
    """Return whether Litestar's router answers `GET path` with a route."""
    try:
        app.asgi_router.handle_routing(path=path, method="GET")
    except HTTPException:
        return False
    return True


Anonymous.__module__ = "grelmicro.integrations.litestar"
Authenticated.__module__ = "grelmicro.integrations.litestar"
current_token.__module__ = "grelmicro.integrations.litestar"
